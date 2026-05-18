# Viewer v2 — Capacidades VTK: Preguntas y Respuestas

> Fecha: 15 mayo 2026  
> Contexto: Análisis de capacidades del visor 3D (viewer_v2) basado en VTK/PyVista para el proyecto KeywordManager (pyDeckGen).

---

## 1. Caras abiertas en piezas con nodos compartidos

**Pregunta:** Para piezas que comparten nodos, al hacer el render en VTK esos espacios compartidos quedan abiertos. ¿Hay una forma de cerrar esas caras abiertas?

**Respuesta:**

### El problema raíz

En LS-DYNA, dos piezas de chapa pueden compartir nodos en su unión (mesh merged). Al renderizar en VTK, el `vtkDataSetSurfaceFilter` **elimina las caras "interiores"** (caras compartidas entre celdas adyacentes). Para mallas volumétricas esto es correcto, pero para **shell elements (2D)**, cada elemento ES una cara de superficie. Al tener dos elementos de partes distintas compartiendo una arista, el filtro los trata como cara interna → la elimina → **hueco visible**.

### Raíz técnica en el pipeline VTK

```
UnstructuredGrid (todo el modelo)
    → vtkDataSetSurfaceFilter   ← aquí se pierden las caras compartidas
    → PolyData por parte
    → Actor
```

### Soluciones posibles

| Opción | Cómo | Ventaja | Riesgo |
|--------|------|---------|--------|
| **A. Construir PolyData directamente desde elementos** | Para shells: cada quad/tri → `VTK_QUAD`/`VTK_TRIANGLE` sin pasar por surface filter | Correcto para shells, sin gaps | Requiere refactor del pipeline de extracción |
| **B. Separar por parte ANTES del surface filter** | Extraer celdas de cada part_id en un UnstructuredGrid propio, luego aplicar surface filter por separado | Cada parte mantiene su frontera completa | Más actors, más memoria |
| **C. `vtkFeatureEdges` + `vtkFillHolesFilter`** | Post-procesado del PolyData para cerrar huecos | Mínimo cambio | `FillHolesFilter` requiere ajustar `HoleSize`, puede rellenar mal |
| **D. Doble cara (backface culling OFF)** | Solo visual, no cierra los huecos reales | Trivial | No resuelve el problema estructural |

### Recomendación

**Opción B** es la más segura: extraer el `UnstructuredGrid` **por part_id** antes de aplicar `vtkDataSetSurfaceFilter`. Cada parte obtiene su propia superficie completa sin que el filtro confunda aristas compartidas con caras internas.

**Opción A** es la más correcta técnicamente (shells son superficies, no volúmenes), pero requiere modificar cómo se construye la geometría VTK desde los DataFrames de PyDyna.

---

## 2. Mostrar coordenadas de un NODE haciendo click

**Pregunta:** ¿Es posible mostrar la coordenada de un NODE haciendo directamente click izquierdo sobre una pieza?

**Respuesta:**

Sí, es posible. VTK tiene mecanismos para eso:

**`vtkCellPicker` / `vtkPointPicker`** — al hacer click izquierdo en el render, el picker convierte las coordenadas 2D de pantalla a coordenadas 3D del modelo y puede devolver:
- el punto 3D exacto del click (coordenada interpolada en la cara)
- el **nodo más cercano** (point ID en el PolyData) y sus coordenadas XYZ

```
MousePressEvent (left click)
    → vtkPointPicker.Pick(x, y, z=0, renderer)
    → GetPickPosition()   → coordenada 3D del punto clickeado
    → GetPointId()        → índice del nodo más cercano en el mesh VTK
    → mapear a NODE ID de LS-DYNA (si se guarda el mapeo índice VTK ↔ NID)
```

La parte más delicada sería **mapear el índice VTK al `NID` real de LS-DYNA**, porque el PolyData tiene sus propios índices 0-based que no coinciden necesariamente con los `NID` del deck. Habría que guardar ese mapeo al construir la geometría.

---

## 3. Mediciones y cotas estilo PMI

**Pregunta:** ¿Es posible hacer mediciones y poner cotas en estilo PMI?

**Respuesta:**

Sí, es posible. VTK tiene los componentes necesarios:

**Mediciones interactivas:**
- `vtkDistanceWidget` + `vtkDistanceRepresentation3D` — distancia entre dos puntos con línea y cota visual, interactivo (click para colocar puntos)
- `vtkAngleWidget` — ángulo entre tres puntos
- `vtkLineWidget2` — línea con handles arrastrables

**Anotaciones estilo PMI:**
- `vtkBillboardTextActor3D` — texto 3D que siempre mira a la cámara (estilo etiqueta PMI)
- `vtkCaptionActor2D` — caption con línea líder (leader line) hacia un punto 3D
- `vtkFollower` + `vtkVectorText` — texto 3D que sigue la cámara

**Flujo típico:**
```
Click punto 1 → Click punto 2
    → vtkDistanceWidget calcula la distancia
    → Dibuja línea de cota con flechas y valor
    → Texto "124.5 mm" flotante en 3D
```

**Limitaciones:**
- VTK da los bloques básicos, pero el estilo visual de PMI "profesional" (como en CATIA/NX) requiere bastante código personalizado para flechas, tolerancias, planos de referencia, etc.
- El `vtkDistanceWidget` out-of-the-box ya se ve bastante bien para un visor FEM.

---

## 4. Planos de corte

**Pregunta:** ¿Qué se puede decir sobre planos de cortes? ¿Cómo quedarían las vistas?

**Respuesta:**

VTK tiene soporte nativo muy completo para planos de corte.

### Componentes VTK disponibles

| Herramienta | Para qué sirve |
|---|---|
| `vtkPlane` + `vtkClipPolyData` | Corta el mesh por un plano matemático |
| `vtkClipDataSet` | Para UnstructuredGrid (mallas FEM completas) |
| `vtkCutter` | Genera la línea/superficie de intersección |
| `vtkImplicitPlaneWidget2` | Plano **interactivo** — arrastrable con el mouse en tiempo real |
| `vtkClipClosedSurface` | Cierra las caras del corte (cap) con polígono sólido |

### Vista interactiva

Con `vtkImplicitPlaneWidget2` el usuario arrastraría el plano en tiempo real:
- Handle central para mover el plano
- Handle de normal para rotarlo
- El modelo se recorta en tiempo real mientras se arrastra

### Limitaciones a considerar

- Para **shells** (chapa fina), el corte muestra una línea, no una sección sólida — visualmente correcto para FEM
- Para **sólidos** (hexaedros), el cap cierra la sección y se ve como un CAD
- Múltiples planos simultáneos son posibles pero requieren composición de filtros

---

## 5. Detección de puntos de control de features

**Pregunta:** ¿Es posible la detección de puntos de control de features como centro de agujeros, radios de curvatura, puntos medios, puntos extremales, etc.?

**Respuesta:**

**Parcialmente sí, con limitaciones importantes.**

### La limitación fundamental

En un modelo LS-DYNA la geometría está **discretizada** — no hay B-rep CAD. No hay "agujero circular perfecto", hay un polígono de N lados. La calidad de la detección depende directamente de la densidad de malla.

### Factibilidad por tipo de feature

| Feature | Método VTK | Calidad |
|---|---|---|
| **Centro de agujero** | `vtkFeatureEdges` (BoundaryEdges) → detecta aristas libres → agrupa en loops → centroide del loop | ★★★ Buena si malla es fina |
| **Puntos extremales** (min/max XYZ) | `polydata.GetBounds()` o iteración de array de puntos | ★★★★★ Exacta |
| **Punto medio entre dos nodos** | Selección de 2 puntos + promedio XYZ | ★★★★★ Exacta |
| **Radio de curvatura / fillets** | `vtkCurvatures` (Gaussian + Mean curvature) → ajuste de círculo | ★★ Aproximado, sensible a malla |
| **Centro de arco/radio** | Fitting circular sobre aristas de alta curvatura (RANSAC o mínimos cuadrados) | ★★ Complejo, aproximado |
| **Aristas de feature** (sharp edges) | `vtkFeatureEdges` con ángulo umbral | ★★★★ Muy bueno |

### Herramientas VTK clave

```python
vtkFeatureEdges
    → BoundaryEdgesOn()   → aristas libres (bordes de agujeros, contornos)
    → FeatureEdgesOn()    → aristas vivas (esquinas, dobles)
    → ManifoldEdgesOn()   → aristas internas de malla

vtkCurvatures
    → SetCurvatureTypeToMean()
    → Alta curvatura media → zona de fillet
```

### Lo que NO es fiable en FEM

- **Radio exacto de un fillet**: si el radio tiene 5mm y la malla tiene elementos de 3mm, el resultado es pobre
- **Center de agujero elíptico vs circular**: el algoritmo no distingue — trata todo como polígono
- **Features con menos de ~6 elementos en el perímetro**: el loop no tiene suficiente información

---

## 6. Feature Recognizer: propiedades geométricas, volumen y área superficial

**Pregunta:** ¿Es posible tener un feature recognizer de una geometría o pieza importada para extraer sus propiedades geométricas, y hacer cálculos de volumen o área superficial?

**Respuesta:**

### Cálculos de área y volumen — SÍ, directamente

**`vtkMassProperties`** (para meshes cerrados):
```python
mass_props = vtk.vtkMassProperties()
mass_props.SetInputData(polydata)
volume        = mass_props.GetVolume()          # cm³, mm³ según unidades del modelo
surface_area  = mass_props.GetSurfaceArea()     # mm²
centroid      = mass_props.GetCenterOfMass()    # XYZ
```

Para **shells (chapa)** el volumen no está bien definido (superficie abierta), pero sí:
```
Área superficial = Σ área de cada elemento (quad/tri)
Volumen de chapa ≈ Área × espesor   (si se conoce *MAT + *SECTION_SHELL)
```
Esto es **exacto** porque los datos de espesor están en el keyword `*SECTION_SHELL`.

### Feature Recognition — limitado pero útil

| Feature | Método | Fiabilidad |
|---|---|---|
| **Planos** (flanges, caras planas) | Agrupar elementos con normal similar (`vtkPolyDataNormals` + clustering) | ★★★★ |
| **Zonas cilíndricas** (agujeros, tubos) | `vtkCurvatures` → curvatura gaussiana constante + loop cerrado | ★★★ |
| **Agujeros / cutouts** | Boundary loops (aristas libres cerradas) | ★★★★ |
| **Simetría** | Bounding box + análisis de distribución de normales | ★★★ |
| **Profundidad de embutición** (draw depth) | Proyección en dirección de troquelado → min/max Z | ★★★★ |
| **Aristas vivas / radios** | `vtkFeatureEdges` + `vtkCurvatures` | ★★★ |

### Lo que NO es posible sin geometría CAD

Un feature recognizer completo tipo CATIA/NX necesita B-rep (NURBS, topología exacta). En FEM:
- No se distingue un agujero circular perfecto de uno hexagonal
- No se pueden extraer parámetros de diseño (radio nominal, tolerancia)
- Los fillets se ven como "zona de alta curvatura", no como "fillet R=3mm"

### Propiedades geométricas calculables de forma fiable

```
✅ Área superficial total
✅ Área por parte (part_id)
✅ Volumen de material (área × espesor del SECTION)
✅ Centroide geométrico
✅ Bounding box (XYZ min/max, dimensiones)
✅ Bounding box orientado (OBB) → orientación principal
✅ Número y área de agujeros (boundary loops)
✅ Longitud de aristas libres (perímetro de contornos)
✅ Momento de inercia de superficie (scipy / cálculo manual)
```

### Panel "Part Properties" propuesto

Para un visor FEM de LS-DYNA, el caso de uso más valioso sería un panel que muestre:
- Área superficial de la parte seleccionada
- Volumen de material (usando espesor del keyword)
- Bounding box y dimensiones
- Número de agujeros detectados
- Área de cada agujero

---

## Resumen de capacidades

| Capacidad | Estado en VTK | Complejidad de implementación |
|---|---|---|
| Caras abiertas (shared nodes) | Soluble (separar por part_id) | Media |
| Coordenadas de nodo por click | Sí — `vtkPointPicker` | Baja |
| Mediciones / cotas PMI | Sí — `vtkDistanceWidget`, `vtkAngleWidget` | Media |
| Planos de corte interactivos | Sí — `vtkImplicitPlaneWidget2` | Media |
| Centro de agujeros | Sí (aprox.) — boundary loops | Media-Alta |
| Puntos extremales / midpoints | Sí — exacto | Baja |
| Radios de curvatura | Parcial (aprox.) — `vtkCurvatures` | Alta |
| Área superficial / volumen | Sí — `vtkMassProperties` | Baja |
| Feature recognition completo | No (requiere CAD B-rep) | N/A |
