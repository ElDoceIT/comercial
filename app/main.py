from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="Comercial - En Construcción")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Comercial - Sitio en Construcción</title>
        <style>
            * {
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }
            body {
                background-color: #0f172a;
                color: #f8fafc;
                font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                display: flex;
                align-items: center;
                justify-content: center;
                min-height: 100vh;
                text-align: center;
                padding: 20px;
            }
            .card {
                background-color: #1e293b;
                border: 1px solid #334155;
                padding: 40px 30px;
                border-radius: 16px;
                box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
                max-width: 480px;
                width: 100%;
            }
            .icon-wrapper {
                background-color: rgba(59, 130, 246, 0.1);
                color: #3b82f6;
                width: 80px;
                height: 80px;
                border-radius: 50%;
                display: flex;
                align-items: center;
                justify-content: center;
                margin: 0 auto 24px auto;
            }
            .icon-wrapper svg {
                width: 40px;
                height: 40px;
            }
            h1 {
                font-size: 1.75rem;
                font-weight: 700;
                margin-bottom: 12px;
                color: #ffffff;
            }
            p {
                color: #94a3b8;
                font-size: 1rem;
                line-height: 1.5;
                margin-bottom: 24px;
            }
            .badge {
                display: inline-block;
                background-color: rgba(234, 179, 8, 0.15);
                color: #fde047;
                border: 1px solid rgba(234, 179, 8, 0.3);
                font-size: 0.85rem;
                padding: 6px 14px;
                border-radius: 20px;
                font-weight: 600;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon-wrapper">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M11.42 15.17 17.25 21A2.652 2.652 0 0 0 21 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 0 1-3.586 0 2.548 2.548 0 0 1 0-3.586l5.653-4.655m7.01-4.01 1.096-1.096A2.652 2.652 0 0 0 17.25 3l-1.096 1.096m0 0L12 8.343M17.25 3.096l-3.03 2.496c-.384.317-.626.74-.766 1.208m0 0L8.343 12" />
                </svg>
            </div>
            <h1>Portal de Comercial</h1>
            <p>Estamos trabajando en el desarrollo del nuevo sistema de Recursos Humanos.</p>
            <div class="badge">🚧 Módulo en construcción</div>
        </div>
    </body>
    </html>
    """

@app.get("/health")
async def health_check():
    return {"status": "ok"}