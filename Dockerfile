FROM python:3.14-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

ENTRYPOINT ["python", "app.py"]

LABEL maintainer="https://github.com/101br03k"
LABEL description="Small Python service that periodically queries a Homebox instance for scheduled maintenance entries and notifies when maintenance is due."
