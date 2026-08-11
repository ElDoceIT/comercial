# Guía de estilos — Calendario C12

## 1. Identidad visual

| Elemento | Especificación |
|---|---|
| Estilo | Interfaz administrativa clara, compacta, con superficies blancas, fondo gris azulado y acento azul |
| Fuente | `"Segoe UI", Tahoma, Geneva, Verdana, sans-serif` |
| Iconos | Bootstrap Icons, trazo simple; usar icono + texto cuando la acción no sea inequívoca |
| Logo fuente | `app/static/logoc12_100.png`, PNG RGBA, `100 × 100 px` |
| Logo en barra | Imagen `24.8 × 24.8 px` (`1.55rem`) dentro de un área `32 × 32 px` |
| Logo grande | Usar el archivo original hasta `100 × 100 px`, `object-fit: contain`; no deformar, recolorear ni recortar |
| Tema | Claro por defecto; oscuro mediante `data-theme="dark"` en `<html>` |

## 2. Variables de diseño

### Colores — tema claro

| Código | Variable / uso |
|---|---|
| `#F6F7FB` | Fondo general alternativo |
| `linear-gradient(180deg, #F8FAFC 0%, #EEF2F7 100%)` | Fondo principal de la app |
| `#FFFFFF` | Tarjetas, menús, campos y superficies |
| `rgba(255,255,255,.92)` | Superficie translúcida |
| `#111827` | Texto principal |
| `#374151` | Texto secundario |
| `#6B7280` | Texto auxiliar y deshabilitado |
| `#E5E7EB` | Borde general |
| `#DBE3F0` | Borde de controles compactos |
| `#2563EB` | Acción primaria, enlace activo, foco |
| `#1D4ED8` | Hover primario y títulos azules |
| `#1E40AF` | Encabezado azul oscuro |
| `#EF4444` | Peligro |
| `#B91C1C` | Hover peligro |
| `#F0FDF4` / `#166534` / `#BBF7D0` | Éxito: fondo / texto / borde |
| `#FEF2F2` / `#991B1B` / `#FECACA` | Error: fondo / texto / borde |
| `#FFF7ED` / `#9A3412` / `#FDBA74` | Advertencia: fondo / texto / borde |

### Colores — tema oscuro

| Uso | Código |
|---|---|
| Fondo general | `#111722` |
| Superficie | `#1A2230` |
| Fondo principal | `linear-gradient(180deg, #101622 0%, #141B28 100%)` |
| Texto principal | `#D9DEE7` |
| Texto secundario | `#B8C1CF` |
| Texto auxiliar | `#8893A3` |
| Borde | `#303B4D` |
| Primario | `#8FB7FF` |
| Hover primario | `#B7CFFC` |
| Campo | `#111827` |
| Tarjeta secundaria | `#1D2635` |

### Radios, sombras y espaciado

| Token | Valor | Uso |
|---|---:|---|
| Radio pequeño | `6px` | Detalles menores |
| Radio campo | `8px` | Inputs, selects y desplegables |
| Radio medio | `10px` | Filas y paneles |
| Radio tarjeta | `12–16px` | Tarjetas; usar `16px` como estándar |
| Radio píldora | `999px` | Botones, filtros, badges y encabezados compactos |
| Sombra suave | `0 2px 10px rgba(0,0,0,.04)` | Tarjetas y barra |
| Sombra control | `0 6px 18px rgba(15,23,42,.06)` | Filtros y controles flotantes |
| Sombra elevada | `0 10px 24px rgba(15,23,42,.08)` | Menús desplegables |
| Separación mínima | `4px` | Entre controles relacionados |
| Separación normal | `12px` | Entre campos y acciones |
| Separación de bloques | `20px` | Entre secciones |

## 3. Tipografía

Base recomendada: `16px`; peso normal `400`; color `#111827`.

| Código | Uso | Tamaño | Peso | Interlineado / detalle |
|---|---|---:|---:|---|
| `T-01` | Título principal `h1` | `24.8px` (`1.55rem`) | `700` | margen superior `0` |
| `T-02` | Título de página/configuración | `18.4px` (`1.15rem`) | `700` | `1.2`, azul `#1E3A8A` |
| `T-03` | Título dentro de encabezado | `13.6–14.4px` | `600–700` | `1.2`, azul |
| `T-04` | Título de tarjeta | `15.7px` (`.98rem`) | `700` | `1.25`, azul `#1D4ED8` |
| `T-05` | Texto de interfaz | `14px` | `600` | navegación, botones principales |
| `T-06` | Texto compacto | `11.2–13px` | `600–700` | tablas, menús y controles |
| `T-07` | Etiqueta de campo | `10px` | `700` | mayúsculas, tracking `.06em`, `#64748B` |
| `T-08` | Eyebrow / categoría | `11.5px` (`.72rem`) | `400` | mayúsculas, tracking `.12em`, `#6B7280` |
| `T-09` | Ayuda / pie | `12.5px` (`.78rem`) | `400–600` | `#6B7280` |

No incorporar fuentes externas. No usar más de tres niveles tipográficos en una misma vista.

## 4. Estructura general

| Elemento | Especificación |
|---|---|
| Contenedor principal | `max-width: 1280px; width: 100%; margin: 0 auto; padding: 17.6px 17.6px 24px` |
| Barra superior | Sticky, `top: 0`, `z-index: 1100`, fondo blanco, borde inferior `#E5E7EB`, padding horizontal `24px` |
| Navegación | Texto `14px/600`; activo y hover `#2563EB`; separadores `1 × 14px` |
| Pie | Centrado, `12.5px/600`, color `#6B7280`, separación `12px` |
| Apilado de páginas | Grid con `20px` de gap |
| Layout calendario | Sidebar `minmax(280px, 20%)` + contenido flexible; gap `19.2px` |
| Tarjeta estándar | Fondo blanco al 90%, borde `1px solid #E5E7EB`, radio `16px`, padding `16px`, sombra suave |
| Tarjeta de configuración | Radio `12px`, borde azul al 14%, padding `17.6px`, sombra tenue |

## 5. Encabezados

### Encabezado de sección `H-01`

| Propiedad | Valor |
|---|---|
| Contenedor | Flex, centrado vertical, contenido separado |
| Padding | `4px 10px` |
| Radio | `999px` |
| Borde | `1px solid rgba(37,99,235,.22)` |
| Fondo | `linear-gradient(180deg, rgba(239,246,255,.95), rgba(219,234,254,.95))` |
| Sombra | `0 4px 10px rgba(29,78,216,.12)` |
| Título | `13.6px`, `700`, color `#1D4ED8` |
| Contador | Alto `24px`, mínimo `32px`, `11px/700`, fondo blanco, borde azul suave, píldora |

### Encabezado de página `H-02`

Usar tarjeta o banda de radio `14px`, padding `10.4px 13.6px`; título `18.4px/700`, color `#1E3A8A`. Puede incluir eyebrow `T-08` encima y acciones alineadas a la derecha.

### Encabezado urgente `H-03`

Fondo `#FEF2F2`, borde `#FECACA`, título `#7F1D1D`, sombra `0 4px 10px rgba(239,68,68,.12)`.

## 6. Campos y formularios

| Componente | Especificación |
|---|---|
| Formulario | Grid, gap `16px` |
| Label | Código `T-07`; arriba del campo con gap `3.2px` |
| Input / select | Ancho `100%`, padding `9.6px 12px`, borde `#D1D5DB`, radio `8px`, fondo blanco |
| Textarea | Padding `12px`, resize vertical |
| Focus | Borde `#2563EB`; halo `0 0 0 3px rgba(37,99,235,.16)`; sin outline nativo |
| Placeholder | Texto auxiliar; en oscuro `#64748B` |
| Ayuda | `12.5px`, color `#6B7280` |
| Error de campo | Fondo `#FEF2F2`, texto `#991B1B`, borde `#FECACA`, radio `8px`, `11.8px/700` |
| Checkbox / radio | `accent-color: #2563EB`; etiqueta `14.4px/700` |
| Acciones del formulario | Flex, alineadas a la derecha, gap `12px`; en móvil cada botón ocupa el mismo ancho |

## 7. Botones

| Código | Uso | Medidas y estilo |
|---|---|---|
| `B-01` | Acción primaria con texto | Alto mínimo `36–40px`, padding `11.2px 16px`, píldora, fondo `#2563EB`, texto blanco `14px/600`; hover `#1D4ED8` |
| `B-02` | Acción secundaria | Fondo `#F8FAFC`, texto `#334155`, borde `#DBE3F2`; hover fondo `#F1F5F9` |
| `B-03` | Acción de icono | `36 × 36px`, circular, fondo blanco, borde `#DBE3F0`, icono `19.5px`; tooltip y `aria-label` obligatorios |
| `B-04` | Icono primario | Como `B-03`; fondo `#EFF6FF`, texto `#1D4ED8`, borde azul al 34% |
| `B-05` | Icono éxito | Fondo `#F0FDF4`, texto `#15803D`, borde verde al 36%; hover `#DCFCE7` |
| `B-06` | Icono peligro | Fondo `#FEF2F2`, texto `#DC2626`, borde rojo al 36%; hover `#FEE2E2` |
| `B-07` | Crear elemento | Píldora blanca, mínimo `190px`, padding `12px 22px 12px 46px`; círculo azul `34 × 34px` superpuesto a la izquierda |
| `B-08` | Control segmentado | Marco blanco con padding/gap `4px`; opción `8px 14px`, `10px/600`; activa con gradiente `#2563EB → #1D4ED8` |

Estados comunes: transición `180ms`; hover puede elevar `-1px`; active `scale(.96–.985)`; disabled `opacity: .62`, sin eventos ni sombra.

## 8. Filtros

| Elemento | Especificación |
|---|---|
| Grupo | Flex, wrap, centrado, gap `7.2px`, alineado abajo |
| Cápsula | Fondo blanco, borde `#DBE3F0`, radio `999px`, padding `4px 6px 4px 10px`, sombra de control |
| Etiqueta | `10px/700`, mayúsculas, tracking `.06em`, `#64748B` |
| Select interno | Mínimo `140px`, padding `7px 27px 7px 10px`, fondo `#F8FBFF`, borde `#DCE3EF`, píldora, `11.2px` |
| Campo de búsqueda | Crece desde `240px`; mismo alto, radio y foco que el select |
| Limpiar filtro | Botón circular con `×`, `16px/600` |

Los filtros deben estar antes de la tabla/listado. En móvil pasan a grid y ocupan todo el ancho disponible.

## 9. Desplegables

### Menú de navegación

| Propiedad | Valor |
|---|---|
| Ancho mínimo | `190px` |
| Posición | Debajo del disparador, alineado a la izquierda, `z-index: 1200` |
| Fondo | `#FFFFFF` |
| Borde / radio | `1px solid #E6EAF0` / `9px` |
| Padding | `14px 6px 6px` |
| Sombra | `0 10px 24px rgba(15,23,42,.08)` |
| Opción | Padding `8px 10px`, radio `7px`, `13px/600` |
| Hover opción | Fondo `#F5F8FC`, texto `#2563EB` |

### Multiselección

Disparador ancho completo, padding `10px`, radio `8px`, borde `#D0D3DA`. Panel con gap superior `6px`, alto máximo `260px`, scroll vertical, misma anchura, sombra `0 8px 20px rgba(0,0,0,.08)`. Cada opción usa padding `8px 10px`, gap `8px`, texto `13px` y divisor `#F1F3F8`.

## 10. Tablas

| Elemento | Especificación |
|---|---|
| Contenedor | Fondo blanco, borde `#E2E8F0`, radio `12px`, `overflow-x: auto` |
| Tabla | `width: 100%`, `border-collapse: collapse` |
| Encabezado | Fondo `rgba(248,251,255,.98)`, texto `11.8px`, mayúsculas, tracking `.06em`, color `#6B7280` |
| Celda | Padding `12.8px 11.2px`, alineación izquierda y vertical centrada |
| Divisor | `1px solid #E5E7EB` en borde inferior |
| Texto principal | `13–14px`, color `#111827` |
| Acciones | A la derecha; botones `B-03` a `B-06`, gap `5.6px` |
| Avatares | `33.6 × 33.6px`, círculo, fondo `#EFF6FF`, texto azul, peso `800` |
| Etiquetas | Píldoras compactas, gap `5.6px` |

En pantallas de hasta `820px`: ocultar `<thead>`; convertir `<tbody>` en grid con gap `11.2px`; cada fila pasa a tarjeta con padding `12px`, radio `10px`; cada `td` muestra su etiqueta mediante `data-label`, `10.9px/900`, mayúsculas.

## 11. Estados, badges y avisos

| Estado | Fondo | Texto | Borde |
|---|---|---|---|
| Éxito / autorizado | `#F0FDF4` | `#166534` | `#BBF7D0` |
| Error / rechazado | `#FEF2F2` | `#991B1B` | `#FECACA` |
| Advertencia | `#FFF7ED` | `#9A3412` | `#FDBA74` |
| Informativo / activo | `#EFF6FF` | `#1D4ED8` | azul al 25% |
| Neutro | `#F8FAFC` | `#475569` | `#E5E7EB` |

Avisos: padding `10.4px 12.8px`, radio `12px`, texto `13.8px/600`. Los avisos globales aparecen arriba a la derecha, máximo `380px`, con sombra elevada.

## 12. Calendario y tarjetas de tarea

| Elemento | Especificación |
|---|---|
| Marco calendario | Tarjeta estándar, gap `11.2px`, superficie blanca translúcida |
| Toolbar | Grid `auto 1fr auto`, gap `11.2px`, divisor inferior `#E6EDF5` |
| Tarjeta de tarea | Borde `#E5E7EB`, radio `10px`, padding `8.8px 9.6px`, fondo `linear-gradient(180deg,#FFFDFA,#F8FBFC)` |
| Título de tarea | `14.7px/500`, interlineado `1.2` |
| Grupo de tareas | Borde, radio `12px`, overflow oculto |
| Resumen de grupo | Padding `11.5px 12.8px`, `13.8px/700`, fondo gris-blanco en gradiente |
| Urgente | Fondo y borde rojos suaves; no usar rojo pleno como superficie grande |

Colores de rol para avatares y panel de usuario:

| Rol visual | Gradiente |
|---|---|
| 1 | `#1E3A8A → #1D4ED8` |
| 2 | `#9A3412 → #EA580C` |
| 3 | `#065F46 → #10B981` |
| 4 | `#581C87 → #A855F7` |
| 5 | `#BE123C → #F43F5E` |
| 6 | `#92400E → #F59E0B` |

## 13. Modales

| Elemento | Especificación |
|---|---|
| Fondo | `rgba(15,23,42,.38)` |
| Panel | Fondo blanco, borde `#E5E7EB`, radio `10–12px`, sombra `0 24px 70px rgba(15,23,42,.32)` |
| Ancho diálogo corto | `min(420px, calc(100vw - 32px))` |
| Cabecera | Padding `16px 17.6px 11.2px`, divisor inferior |
| Cuerpo | Padding horizontal `17.6px` |
| Acciones | Fondo `#F8FAFC`, padding `17.6px`, alineadas a la derecha |

Cerrar con `×` o icono, fondo transparente, tamaño visual `20.8px`, `aria-label="Cerrar"`. Bloquear scroll del body mientras el modal esté abierto.

## 14. Responsive

| Corte | Comportamiento |
|---:|---|
| `≤ 1100px` | Reducir composiciones anchas y permitir wrap |
| `≤ 980px` | Reorganizar toolbar, paneles y grillas secundarias |
| `≤ 820px` | Ocultar navegación de escritorio; mostrar menú móvil; una columna; tablas como tarjetas; botones de formulario flexibles |

Reglas obligatorias: no generar scroll horizontal de página; mantener controles táctiles de al menos `36px`; permitir wrap en barras de acciones; conservar `16px` de margen lateral aproximado.

## 15. Accesibilidad y consistencia

- Contraste mínimo WCAG AA.
- Todo control debe tener estado `hover`, `focus-visible`, `active` y `disabled`.
- Botón solo con icono: `aria-label` y `title` obligatorios.
- No comunicar estados solo mediante color: acompañar con texto o icono.
- Mantener orden: título, descripción breve si es necesaria, filtros, contenido, acciones.
- Usar los mismos códigos `T`, `H` y `B` en todas las apps.
- No introducir otra fuente, otro azul principal, esquinas rectas ni sombras fuertes.
- No usar gradientes fuera de fondo general, encabezados azules, roles y controles activos.

## 16. CSS base reutilizable

```css
:root {
  --c-bg: #f6f7fb;
  --c-surface: #ffffff;
  --c-text: #111827;
  --c-text-2: #374151;
  --c-muted: #6b7280;
  --c-border: #e5e7eb;
  --c-control-border: #dbe3f0;
  --c-primary: #2563eb;
  --c-primary-hover: #1d4ed8;
  --c-danger: #ef4444;
  --font-sans: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
  --r-field: 8px;
  --r-panel: 12px;
  --r-card: 16px;
  --r-pill: 999px;
  --shadow-soft: 0 2px 10px rgba(0, 0, 0, .04);
  --shadow-control: 0 6px 18px rgba(15, 23, 42, .06);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: var(--font-sans);
  color: var(--c-text);
  background: linear-gradient(180deg, #f8fafc 0%, #eef2f7 100%);
}
.card {
  padding: 1rem;
  border: 1px solid var(--c-border);
  border-radius: var(--r-card);
  background: rgba(255,255,255,.9);
  box-shadow: var(--shadow-soft);
}
.control {
  width: 100%;
  padding: .6rem .75rem;
  border: 1px solid #d1d5db;
  border-radius: var(--r-field);
  background: var(--c-surface);
  color: var(--c-text);
  font: inherit;
}
.control:focus-visible {
  outline: 0;
  border-color: var(--c-primary);
  box-shadow: 0 0 0 3px rgba(37,99,235,.16);
}
.btn {
  min-height: 36px;
  padding: .7rem 1rem;
  border: 1px solid transparent;
  border-radius: var(--r-pill);
  background: var(--c-primary);
  color: #fff;
  font: 600 14px var(--font-sans);
  cursor: pointer;
  transition: .18s ease;
}
.btn:hover { background: var(--c-primary-hover); }
.btn:active { transform: scale(.985); }
.btn:disabled { opacity: .62; pointer-events: none; }
```

## 17. Instrucción breve para Codex

> Replicar exactamente la estética definida en `GUIA_DE_ESTILOS.md`. Usar Segoe UI y los tokens indicados; fondo gris azulado, tarjetas blancas con bordes suaves, azul `#2563EB`, controles y encabezados tipo píldora. Aplicar los códigos tipográficos `T-01` a `T-09`, encabezados `H-01` a `H-03` y botones `B-01` a `B-08`. Mantener tablas, filtros, desplegables, estados, modo oscuro y responsive según la guía. Reutilizar `logoc12_100.png` sin modificar su proporción. No crear variantes visuales fuera de esta especificación.
