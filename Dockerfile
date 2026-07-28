FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias
RUN pip install --no-cache-dir fastapi uvicorn

# Copiar TODO el contenido del proyecto (o la carpeta app)
COPY ./app ./app

# Ejecutar apuntando al modulo app.main:app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8052"]