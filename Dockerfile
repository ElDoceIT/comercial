FROM python:3.11-slim

# Evita que Python genere archivos .pyc y envía logs directamente a la consola
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Copiar e instalar las dependencias del proyecto
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. Copiar únicamente el código fuente de la aplicación
COPY ./app ./app

# 3. Comando para levantar FastAPI
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8052"]