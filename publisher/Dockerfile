FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://pypi.org/simple --timeout 60 --retries 5 -r requirements.txt

COPY main.py .
CMD ["python", "-u", "main.py"]