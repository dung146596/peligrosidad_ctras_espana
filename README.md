# 🚗 Peligrosidad Vial España (DGT + PostGIS)

Herramienta de código abierto de alto rendimiento para la **visualización de mapas de calor de accidentalidad** y el **análisis de riesgo en rutas por carretera** en España, construida con microdatos oficiales de la Dirección General de Tráfico (DGT) y el Instituto Geográfico Nacional (IGN).

---

## 📐 Modelo Metodológico del Índice de Riesgo

El cálculo de la peligrosidad vial en tramos y rutas combina la severidad de las víctimas con la persistencia histórica de los accidentes, inspirándose en los métodos de auditoría de seguridad vial del **Programa Europeo de Evaluación de Carreteras (EuroRAP / iRAP)** y las metodologías de identificación de Tramos de Concentración de Accidentes (TCA) empleadas por el Ministerio de Transportes y Movilidad Sostenible de España.

### 1. Ponderación por Severidad (Índice de Gravedad EuroRAP)
Cada accidente registrado en la base de datos recibe un **peso de severidad** ($S$) según los daños a la salud de las personas involucradas:

$$\text{Peso de Severidad } (S) = (10 \times \text{Fallecidos}) + (3 \times \text{Heridos Graves}) + (1 \times \text{Heridos Leves})$$

* **Fallecidos (a 30 días):** Ponderación de **10.0** (Máxima gravedad en el estándar DGT).
* **Heridos Graves (Hospitalizados):** Ponderación de **3.0**.
* **Heridos Leves (No hospitalizados):** Ponderación de **1.0**.

### 2. Análisis Espacial de Rutas (Buffer PostGIS)
Dado el trazado geométrico de una ruta de navegación $R$ (obtenida como una `LineString` GeoJSON a través de OSRM):
1. Se genera una zona de influencia espacial (*buffer*) de **~500 metros** ($\approx 0.005^{\circ}$) a lo largo del trazado.
2. Se consultan mediante indexación espacial GiST en PostGIS (`ST_DWithin`) todos los accidentes históricos contenidos dentro de dicho área.

### 3. Índice de Riesgo Promedio Anual
Para evitar la volatilidad estadística de años aislados y homogeneizar el riesgo independientemente del número de ejercicios analizados ($N_{\text{años}}$ en la muestra, ej. 2020–2024), se calcula el **Índice de Riesgo Anual Promedio** ($R_{\text{anual}}$):

$$R_{\text{anual}} = \frac{\sum_{i=1}^{M} S_i}{\max(1, N_{\text{años}})}$$

Donde $M$ es el total de accidentes acumulados a lo largo del trayecto.

#### Criterios de Clasificación de Riesgo en Ruta
* 🟢 **Bajo:** $R_{\text{anual}} \le 10$
* 🟠 **Medio:** $10 < R_{\text{anual}} \le 30$
* 🔴 **Alto:** $R_{\text{anual}} > 30$

---
### Ejemplo 1 de análisis de riesgo en ruta:
![Mapa de calor de accidentalidad vial](img/sample_1.png)

### Ejemplo 2 de análisis de riesgo en ruta:
![Análisis de riesgo en ruta](img/sample_2.png)

---

## 🛠️ Arquitectura y Funcionamiento General

El sistema consta de tres capas principales:

1. **Pipeline de Ingesta e Indexación Espacial (`ingest_dgt.py`):**
   * Lee los archivos de microdatos en CSV de la DGT (2020–2024).
   * Normaliza los códigos de carreteras y puntos kilométricos (PKs).
   * Asocia cada accidente con la capa georreferenciada de la Red Viaria del IGN (`rt_ppkk_p`) mediante `pyogrio` para obtener coordenadas exactas (WGS84 / EPSG:4326).
   * Vuelca los registros indexados en PostgreSQL/PostGIS asociándolos a la columna `anio`.

2. **Backend de Servicios REST (`FastAPI`):**
   * `GET /api/v1/accidentes/bbox`: Devuelve puntos de siniestralidad filtrados por la ventana de visión (*Bounding Box*) del mapa.
   * `POST /api/v1/rutas/analizar`: Recibe una geometría de ruta, efectúa la intersección espacial en PostGIS y devuelve el resumen de severidad acumulada, el promedio anual y el desglose de causas y tipologías.

3. **Frontend Interactivo (`MapLibre GL JS` + `OSRM` + `Nominatim`):**
   * Interfaz fluida basada en MapLibre GL JS con renderizado en tiempo real de mapas de calor (*heatmap*).
   * Integración con geocodificación de municipios mediante OpenStreetMap Nominatim.
   * Cálculo del trazado mediante el motor de enrutamiento OSRM (Open Source Routing Machine).

---

## 📋 Créditos y Fuentes de Terceros

Este proyecto hace uso de datos abiertos, APIS y servicios públicos proporcionados por las siguientes entidades:

* **[Dirección General de Tráfico (DGT)](https://www.dgt.es/):** Microdatos oficiales de accidentes de tráfico con víctimas (serie histórica 2020–2024).
* **[Instituto Geográfico Nacional (IGN) / CNIG](https://www.cnig.es/):** Capa vectorial de la Red Viaria de España (`rt_viaria.gpkg`), específicamente la capa de hitos kilométricos `rt_ppkk_p`.
* **[EuroRAP / iRAP](https://irap.org/):** Marco metodológico de ponderación de gravedad de víctimas por siniestro vial.
* **[OpenStreetMap / Nominatim](https://nominatim.openstreetmap.org/):** Servicio de geocodificación gratuita de topónimos y municipios de España.
* **[OSRM (Open Source Routing Machine)](http://project-osrm.org/):** Motor de enrutamiento para el cálculo de la geometría vial entre origen y destino.
* **[CARTO](https://carto.com/):** Cartografía base vectorial estilo Positron.

---

## 🚀 Instalación y Despliegue Local

### Requisitos previos
* Python 3.12+
* Docker y Docker Compose

### Pasos
1. **Clonar el repositorio:**
   ```bash
   git clone [https://github.com/dung146596/peligrosidad_ctras_espana.git](https://github.com/dung146596/peligrosidad_ctras_espana.git)
   cd peligrosidad_ctras_espana
2. **Iniciar la DB PostGIS:**
    ```bash
    docker compose up -d

3. **Descargar y meter archivos de datos:**
    Omitidos en el repo por cuestiones de tamaño de ficheros.

    Capa Geográfica de la Red Viaria (IGN / CNIG):
    * **Fuente:** Centro de Descargas del CNIG ([cnig.es](https://www.cnig.es/)).
    * **Producto:** Red Transportes (Información Geográfica de Referencia).
    * **Archivo requerido:** Descargar la capa en formato GeoPackage y renombrarla o colocarla en la raíz del proyecto con el nombre **`rt_viaria.gpkg`**. Debe contener la capa vectorial `rt_ppkk_p` (Puntos Kilométricos).

    Microdatos de Accidentes de Tráfico (DGT):
    * **Fuente:** Portal de Microdatos de la DGT ([dgt.es](https://www.dgt.es/)).
    * **Producto:** Tablas anuales de *Accidentes con Víctimas*.
    * **Archivos requeridos:** Descargar los archivos XLSX y convertir a CSV, guardándolos como TABLA_ACCIDENTES_20.csv, ..., TABLA_ACCIDENTES_24.csv.

4. **Instalar dependencias de Python:**
    ```bash
    pip install -r requirements.txt
5. **Ejecutar el proceso de ingesta:**
    ```bash
    python ingest_dgt.py

6. **Iniciar la aplicación:**
    ```bash
    uvicorn main:app --reload
Accede a la aplicación en http://localhost:8000.

### Licencia
Este proyecto está distribuido bajo la Licencia MIT. Consulta el archivo LICENSE para más detalles.