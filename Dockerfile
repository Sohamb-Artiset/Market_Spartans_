FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium ONCE during build — not at every startup
RUN playwright install chromium

COPY . .

CMD ["python", "main.py"]