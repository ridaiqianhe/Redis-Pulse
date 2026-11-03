# -*- coding:utf-8 -*-
from flask import Flask, jsonify, request
import time
import os
import random
import redis

# Redis 连接设置
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
DATA_TTL_SECONDS = 900
MIN_READY_COUNT = 30
READ_RANDOM_WINDOW = 100
MAX_QUEUE_LENGTH = 3000
CLEANUP_INTERVAL_SECONDS = 30
pool = redis.ConnectionPool(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=3,
    health_check_interval=30,
)
r = redis.Redis(connection_pool=pool)

COUNT_ACTIVE_SCRIPT = r.register_script("""
local cutoff = tonumber(ARGV[1]) - tonumber(ARGV[2])
local counts = {}
for i = 1, #KEYS, 2 do
    redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
    redis.call('ZREMRANGEBYSCORE', KEYS[i + 1], '-inf', cutoff)
    counts[#counts + 1] = redis.call('ZCARD', KEYS[i])
end
return counts
""")


def count_active_data(*names):
    """原子清理过期库存和去重记录，再返回有效数量；不取走有效数据。"""
    keys = [key for name in names for key in (name, f'{name}:seen')]
    counts = COUNT_ACTIVE_SCRIPT(
        keys=keys, args=[int(time.time()), DATA_TTL_SECONDS],
    )
    return dict(zip(names, map(int, counts)))


READ_AND_REMOVE_SCRIPT = r.register_script("""
local key = KEYS[1]
local seen_key = KEYS[2]
local cleanup_key = KEYS[3]
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local min_ready = tonumber(ARGV[3])
local force = ARGV[4]
local window = tonumber(ARGV[5])
local offset = tonumber(ARGV[6])
local cleanup_interval = tonumber(ARGV[7])

if redis.call('SET', cleanup_key, '1', 'NX', 'EX', cleanup_interval) then
    redis.call('ZREMRANGEBYSCORE', key, '-inf', now - ttl)
    redis.call('ZREMRANGEBYSCORE', seen_key, '-inf', now - ttl)
end

local count = redis.call('ZCARD', key)
if count == 0 then
    return {0, count, ''}
end

if count <= min_ready and force ~= '1' then
    return {0, count, ''}
end

local window_count = window
if count < window_count then
    window_count = count
end

local start = count - window_count
local index = start + (offset % window_count)
local values = redis.call('ZRANGE', key, index, index)
if #values == 0 then
    return {0, count, ''}
end

local removed = redis.call('ZREM', key, values[1])
if removed == 1 then
    return {1, count - 1, values[1]}
end

return {0, redis.call('ZCARD', key), ''}
""")

WRITE_UNIQUE_SCRIPT = r.register_script("""
local queue_key = KEYS[1]
local seen_key = KEYS[2]
local cleanup_key = KEYS[3]
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local max_len = tonumber(ARGV[3])
local value = ARGV[4]
local cleanup_interval = tonumber(ARGV[5])

if redis.call('SET', cleanup_key, '1', 'NX', 'EX', cleanup_interval) then
    redis.call('ZREMRANGEBYSCORE', queue_key, '-inf', now - ttl)
    redis.call('ZREMRANGEBYSCORE', seen_key, '-inf', now - ttl)
end

if redis.call('ZSCORE', seen_key, value) then
    return {0, redis.call('ZCARD', queue_key)}
end

redis.call('ZADD', seen_key, now, value)
redis.call('ZADD', queue_key, now, value)
redis.call('ZREMRANGEBYRANK', queue_key, 0, -(max_len + 1))

return {1, redis.call('ZCARD', queue_key)}
""")


class Config(object):
    JSON_AS_ASCII = False


app = Flask(__name__)
app.config.from_object(Config)


def read_unique_data(
        name,
        force=False,
        rmtime=DATA_TTL_SECONDS,
        min_ready=MIN_READY_COUNT,
        window=READ_RANDOM_WINDOW,
        cleanup_interval=CLEANUP_INTERVAL_SECONDS):
    """原子读取并删除一条数据，避免并发请求读到同一个值。"""
    ok, count, value = READ_AND_REMOVE_SCRIPT(
        keys=[name, f'{name}:seen', f'{name}:cleanup'],
        args=[
            int(time.time()),
            rmtime,
            min_ready,
            '1' if force else '0',
            window,
            random.randrange(window),
            cleanup_interval,
        ],
    )
    if int(ok) != 1:
        return jsonify({"count": int(count), 'msg': int(count)})

    token, validate = value.split('###', 1)
    return jsonify({"token": token, "validate": validate, "count": int(count)})


def write_unique_data(
        name,
        rmtime=DATA_TTL_SECONDS,
        max_len=MAX_QUEUE_LENGTH,
        cleanup_interval=CLEANUP_INTERVAL_SECONDS):
    """写入去重数据；TTL 窗口内相同 token/validate 只允许入队一次。"""
    try:
        token = request.form.get('token')
        validate = request.form.get('validate')
        if not token or not validate:
            return jsonify({'code': 0, 'msg': '参数缺失'})

        timestamp = int(time.time())
        data = f"{token}###{validate}"

        try:
            inserted, count = WRITE_UNIQUE_SCRIPT(
                keys=[name, f'{name}:seen', f'{name}:cleanup'],
                args=[timestamp, rmtime, max_len, data, cleanup_interval],
            )
        except redis.exceptions.ResponseError as e:
            if 'WRONGTYPE' in str(e):
                r.delete(name)
                r.delete(f'{name}:seen')
                r.delete(f'{name}:cleanup')
                inserted, count = WRITE_UNIQUE_SCRIPT(
                    keys=[name, f'{name}:seen', f'{name}:cleanup'],
                    args=[timestamp, rmtime, max_len, data, cleanup_interval],
                )
            else:
                return jsonify({'code': 0, 'msg': f'Redis错误: {str(e)}'})

        return jsonify({'code': 1, 'count': int(count), 'inserted': int(inserted)})
    except Exception as e:
        return jsonify({'code': 0, 'msg': f'系统错误: {str(e)}'})

@app.route('/api/slideread', methods=['GET'])
def slideread():
    """读取 slide 数据"""
    type_ = request.values.get('type')
    return read_unique_data('slides', force=str(type_) == '1')

@app.route('/api/slidewrite', methods=['POST'])
def slidewrite():
    """写入 slide 数据"""
    return write_unique_data('slides')





@app.route('/api/yidunread', methods=['GET'])
def yidunread():
    """读取 yidun 数据"""
    type_ = request.values.get('type')
    return read_unique_data('yiduns', force=str(type_) == '1')


@app.route('/api/yidunwrite', methods=['POST'])
def yidunwrite():
    """写入 yidun 数据"""
    return write_unique_data('yiduns')


@app.route('/api/counts', methods=['GET'])
def counts():
    """清理过期数据后读取 slide 和 yidun 有效数量。"""
    return jsonify({'code': 1, **count_active_data('slides', 'yiduns')})


@app.route('/api/readnum', methods=['GET'])
def readnum():
    """读取指定类型的数据数量"""
    type_ = request.values.get('type')
    if not type_:
        return jsonify({'code': 0, 'msg': '参数缺失'})
    if type_ not in ['slides', 'yiduns']:
        return jsonify({'code': 0, 'msg': '类型错误'})
    r_len = count_active_data(type_)[type_]
    return jsonify({'code': 1, 'count': r_len})


@app.errorhandler(404)
def error_date(error):
    """处理 404 错误"""
    return '''Welcome to redis-api
    ''', 404


if __name__ == '__main__':
    app.run(port=10000, host="0.0.0.0", debug=False)
