# Setup exacto

## A. Preparar el Sheet

1. Crea o abre un Google Sheet.
2. Abre Extensions -> Apps Script.
3. Copia free_pipeline/Code.gs.
4. Guarda.
5. Ejecuta setup() manualmente una vez.
6. Concede los permisos solicitados.
7. Recarga el Sheet.
8. Debe aparecer el menú UGC Automation.

La hoja Videos se crea automáticamente.

## B. Primera fila

Rellena una fila:

No = 1
Product = nombre del producto
Product Photo = URL pública o URL/ID de archivo de Google Drive
ICP = por ejemplo: mujeres 25-40 que entrenan 3 veces por semana
Product Features = 2-4 beneficios reales
Video Setting = por ejemplo: home gym
Model = AUTO
Status = READY

Cuando cambies Status a READY, Apps Script crea automáticamente un job en Drive.

## C. Colab

1. Abre Google Colab.
2. Selecciona un runtime GPU cuando esté disponible.
3. Sube free_pipeline/colab_worker.py.
4. Antes de ejecutar el worker, establece UGC_SPREADSHEET_ID al ID de tu Sheet.

Ejemplo de celda:

import os
os.environ["UGC_SPREADSHEET_ID"] = "TU_SPREADSHEET_ID"

5. Ejecuta el worker.

El worker leerá los demás valores desde Config.

## D. Test

Haz una sola fila.

Estado esperado:

READY -> QUEUED -> PROCESSING -> FINISHED

El MP4 aparecerá en My Drive / UGC_Automation / Output.

Finished Video recibirá el enlace al archivo.

## E. Si Colab no entrega GPU

No es un error del código. Google no garantiza GPU en el nivel gratuito y sus límites/hardware cambian dinámicamente.

En ese caso se vuelve a ejecutar el worker cuando haya un runtime GPU disponible.
