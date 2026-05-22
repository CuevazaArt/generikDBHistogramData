# anti_louise_lucky

Variante de `anti_louise` que añade compras cuando el precio alcanza un
máximo local de la ventana `lucky_window` (o el `ha_high` previo).

## Tesis

- Si las entradas oportunistas en mínimos locales mejoran el costo
  promedio en bots long-only, su simétrico debería mejorar el desempeño
  de bots invertidos: comprar en techos locales acumula a mejor precio
  el "short simulado".

## Decisiones

- Lucky strikes no actualizan `last_short_anchor` para no contaminar la
  cadena principal de subidas.
- Permanece long-only en spot, igual que `anti_louise`.

## Observaciones

- Si el mercado no marca nuevos máximos locales se comporta idéntico a
  `anti_louise`.
- Útil para benchmark de simetría con `louise_lucky`.
