FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir youtube-transcript-api
EXPOSE 8080
CMD ["python3", "server.py"]
