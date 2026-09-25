# Calendario macro · confirmador

Confirma cada día qué publicaciones del calendario efectivamente salieron.

## Archivos
- `confirmador.py`: todo el sistema en un archivo (métodos de confirmación, vigilancia de calendarios, reporte y autotest).
- `data/eventos.json`, `data/catalogo.json`, `data/feriados.json`: la base del calendario.
- `data/estado.json`: lo que escribe el confirmador (se crea en la primera corrida).
- `data/reporte.md`: el reporte de la última corrida.
- `.github/workflows/confirmar.yml`: corre a las 09:00 y 19:30 (hora argentina).

## Puesta en marcha
1. Crear un repositorio privado en GitHub y subir esta carpeta.
2. En Actions, correr "Confirmar publicaciones" a mano con "Probar sin escribir" activado y leer el log.
3. Si el log está bien, dejarlo correr solo.
4. Opcional: cargar los secretos `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID` para recibir el reporte.

## Cómo falla (a propósito)
- Una fuente caída no frena la corrida: se prueba el siguiente método y queda registrada en el log.
- Un método que falla 3 corridas seguidas aparece en el reporte como "método caído".
- Si el propio script tiene un error, el autotest o la corrida fallan y GitHub te manda un mail.
- Un evento confirmado nunca vuelve atrás.
- El estado se escribe de forma atómica: nunca queda un archivo a medio escribir.

## Probar localmente
    pip install requests
    python confirmador.py --selftest
    python confirmador.py --dry-run
