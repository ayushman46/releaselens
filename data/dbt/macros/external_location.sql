{#
  Where an `external` model is written (overrides dbt-duckdb's macro of the same name).

    meta.rl_output = 'marts'    -> <output_root>/marts/<model>/run_id=<run_id>/part-00000.parquet
    meta.rl_output = 'features' -> <output_root>/features/release_features/
                                     schema=<feature_schema_version>/run_id=<run_id>/part-00000.parquet

  One file per model and run; a re-run with the same run_id overwrites it with identical bytes
  (deterministic models, fixed row-group size and compression). rl_pipeline.dbt validates that
  run_id and feature_schema_version are path-safe and pre-creates local directories.
#}
{%- macro external_location(relation, config) -%}
  {%- set kind = (config.get('meta') or {}).get('rl_output', 'marts') -%}
  {%- set root = var('output_root') | string -%}
  {%- set run_id = var('run_id') | string -%}
  {%- if kind == 'features' -%}
    {{- root.rstrip('/') }}/features/release_features/schema={{ var('feature_schema_version') }}/run_id={{ run_id }}/part-00000.parquet
  {%- elif kind == 'marts' -%}
    {{- root.rstrip('/') }}/marts/{{ relation.identifier }}/run_id={{ run_id }}/part-00000.parquet
  {%- else -%}
    {{- exceptions.raise_compiler_error("unknown meta.rl_output " ~ kind ~ " on " ~ relation.identifier) -}}
  {%- endif -%}
{%- endmacro -%}
