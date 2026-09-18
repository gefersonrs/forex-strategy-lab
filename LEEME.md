# Forex Strategy Lab

Laboratorio local para formular hipótesis, cambiar estrategias y comparar resultados. Los valores de la conversación —$500, EUR/USD, 4H, riesgo de 0.5% y objetivo 3R— son una plantilla editable.

## Abrir

Haz doble clic en **Abrir laboratorio.cmd**. Se abrirá el laboratorio en el navegador de esta computadora. Los históricos y los experimentos se guardan en la carpeta `data`. El servidor escucha solamente en `127.0.0.1`; no necesita una cuenta de broker.

## Primer experimento

1. En **Datos**, selecciona un histórico. El conjunto de demostración contiene precios sintéticos y está identificado como tal.
2. En **Estrategia**, elige una plantilla o modifica la señal en Python. Ajusta capital, compras/ventas, temporalidad, riesgo, stops, objetivos, filtros, semilla y costos.
3. Ejecuta el backtest. Mira balance, equity, drawdown y operaciones, incluidos los motivos para rechazar entradas por riesgo o margen.
4. Guarda una hipótesis y un nombre en **Experimentos**. Cada guardado conserva el resultado, la configuración, el código y el origen del histórico.
5. Cambia una variable, vuelve a ejecutar y compara ambos experimentos. Monte Carlo, las pruebas de costos y la matriz ATR/ruptura sirven para explorar sensibilidad.

Las plantillas A–E reproducen la secuencia de la conversación: A sin los cuatro filtros adicionales, B añade tendencia diaria, C añade ADX, D añade el margen de ruptura de 0.1 ATR y E añade break-even a 1.5R. El capital y el riesgo que hayas elegido se conservan. Las estrategias son editables; las plantillas no son recomendaciones de parámetros.

La biblioteca incluye dos experimentos de demostración con $10,000 de capital sintético para explorar operaciones, gráficos y comparaciones. La plantilla inicial conserva $500. Con poco capital, un lote mínimo de 1,000 unidades y stops amplios, un resultado sin operaciones puede ser correcto: consulta las entradas omitidas.

En Robustez puedes congelar código y reglas, evaluar el primer tramo y revelar después el período reservado. El segundo tramo reinicia el capital y utiliza el anterior solo para calentamiento; la separación no evita el sesgo si ya inspeccionaste ese mismo histórico. Las reglas congeladas se conservan durante la sesión; guarda sus resultados como experimentos para conservarlos después.

## Datos reales

El importador acepta CSV OHLC con fecha y columnas `open,high,low,close`, o precios bid y ask en columnas separadas. Admite EUR/USD, GBP/USD, AUD/USD, NZD/USD, USD/JPY, USD/CAD y USD/CHF con cuenta expresada en USD. Especifica qué tipo de precio contiene el archivo y qué par representa. Las fechas sin zona se interpretan como UTC; una zona incorrecta cambia la formación de las velas diarias y de 4H.

Ejemplo mínimo de precios **bid**, con la hora de apertura de cada vela:

```csv
time,open,high,low,close
2024-01-02T00:00:00Z,1.10420,1.10450,1.10400,1.10430
2024-01-02T00:01:00Z,1.10430,1.10470,1.10410,1.10460
```

El formato bid/ask incluye `bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close`. Si falta ask, el spread configurado es un supuesto; no se representa como spread histórico observado.

[La exportación histórica oficial de Dukascopy](https://www.dukascopy.com/swiss/english/fx-market-tools/historical-data/) es una fuente para importar archivos. La descarga integrada depende de la disponibilidad del servicio público y puede recibir límites de tráfico. Los fallos se muestran y nunca se sustituyen por datos inventados. El acceso S3 con cobro al solicitante no se utiliza.

Un filtro diario de EMA 200 necesita al menos 200 velas diarias anteriores para calentamiento. Los datos de 1 minuto permiten examinar la ejecución dentro de una señal de 4H. Elegir 1 minuto como señal sobre un archivo horario no crea datos intrabar.

## Qué significan los resultados

- **Balance**: efectivo después de las comisiones cobradas y los resultados realizados.
- **Equity**: balance más el valor de liquidación de la posición abierta.
- **PnL neto** y **R**: incluyen los costos modelados. El spread y el slippage incorporados en los precios no se descuentan dos veces.
- **Drawdown**: caída desde un máximo previo de equity, medida en los puntos de observación de los datos de ejecución. No es una medición tick a tick.
- **Monte Carlo**: remuestreo de operaciones del experimento. Describe escenarios condicionados a esa muestra; no prueba una ventaja futura.

## Supuestos de la ejecución

Señales sobre velas cerradas, entrada a mercado en la siguiente vela disponible, una posición simultánea, sin ejecuciones parciales. Cuando stop y objetivo aparecen dentro de la misma vela y no hay ticks que permitan ordenar los eventos, se aplica stop primero y se cuenta la ambigüedad. El break-even se hace efectivo desde la siguiente vela, evitando asumir retroactivamente un recorrido favorable. Un stop puede perder más de lo presupuestado ante un salto de precio.

Los parámetros de comisión, spread supuesto, slippage, swap y apalancamiento son escenarios configurables. El valor inicial de comisión proviene del ejemplo de la conversación; no certifica las condiciones de una cuenta concreta. [Pepperstone publica sus costos y condiciones](https://pepperstone.com/en/trading/costs-and-fees), que dependen de plataforma, entidad y cuenta. El swap por día es una aproximación configurable, sin calendario completo de financiación del broker.

La primera versión utiliza archivos comprimidos y metadatos locales. Parquet/DuckDB, descarga de ticks para resolver ambigüedades, conexiones cTrader/OANDA, ejecución en vivo y modelos completos de liquidación por margen son ampliaciones separadas. La información mostrada identifica lo que el motor calcula actualmente.

## Código y pruebas

El servidor y el motor se encuentran en esta carpeta. Se requiere Python 3.11 o superior; el iniciador encuentra primero el Python incluido con Codex. El editor modifica la función de señales con un subconjunto de Python documentado dentro de la aplicación; no ejecuta comandos del sistema ni instala bibliotecas desde el editor.

Las pruebas del motor, importación y API están en `tests`. Pueden ejecutarse con `python -m unittest discover -s tests -v` desde esta carpeta.
