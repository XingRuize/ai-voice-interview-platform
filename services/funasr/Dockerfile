FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir funasr fastapi uvicorn python-multipart
RUN pip install --no-cache-dir kaldi-native-fbank

EXPOSE 8000
CMD ["funasr-server", "--host", "0.0.0.0", "--port", "8000", "--model", "sensevoice", "--device", "cpu"]
