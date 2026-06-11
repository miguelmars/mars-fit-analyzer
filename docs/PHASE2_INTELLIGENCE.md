# Phase 2 Intelligence

## `GET /gpt/correlaciones?sport=cycling`

Returns four personal historical associations:

- Rest interval before the next session.
- Previous-week sport volume versus next-week efficiency.
- Temperature cost in BPM while controlling for speed.
- Measured weight versus sport-specific efficiency.

Every result includes sample size and an evidence level. These are
observational associations, not proof of causality.

## `GET /gpt/tendencia?sport=cycling`

Compares the latest four completed weeks with the four completed weeks before
them using:

- Sport-specific efficiency.
- Duration-weighted sport-specific heart rate.
- Sport-specific weekly hours.

Possible states:

- `mejorando`
- `respuesta_positiva_carga_alta`
- `carga_en_observacion`
- `regreso_tras_pausa`
- `pico_atipico`
- `estable`
- `retrocediendo`
- `posible_sobrecarga`

The current incomplete week is returned separately and is not used in the
four-versus-four classification.

## Important Limits

- Heat analysis controls for speed, but not route, wind or elevation.
- Heat confidence also depends on model R2, not only sample size.
- Weight uses only weeks with an actual measurement.
- Counterintuitive weight associations are marked as confounded and are not
  exposed as recommendations.
- Weight analysis uses within-year differences so distant training eras are
  not compared directly.
- Volume increases above 50% trigger a load warning even when efficiency rises.
- Volume increases above 200% are labelled as a return after a pause or an
  atypical spike instead of being shown as a context-free percentage.
- Z2 remains estimated until historical telemetry is fully linked.
- The trend is an athletic signal, not a medical diagnosis.

## Multisport Status

`GET /gpt/athletic-status` combines every weekly discipline into a transparent
equivalent-hours workload proxy. It reports:

- four-week load change
- active-day and session change
- active-week continuity
- recent pauses and load spikes
- transitions between dominant disciplines
- a simple next-step recommendation

The Progress screen exposes three modes: General, Cycling and Running. General
uses the multisport workload; the sport modes retain sport-specific efficiency,
best-form and correlation readings.
