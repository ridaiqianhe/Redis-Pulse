FROM python:3.10-slim-bullseye

WORKDIR /app

COPY requirements.txt /app/

RUN mkdir -p /app/logs \
    && python3 -m pip install --upgrade pip \
    && pip3 install --no-cache-dir -r requirements.txt \
    && rm -rf /tmp/* /root/.cache/*

COPY ./*.py /app/

CMD ["gunicorn", "-c", "gunicorn.py" , "views:app"]
