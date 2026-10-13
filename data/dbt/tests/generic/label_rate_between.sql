{#
  Sanity bounds for the share of positive labels (label = 1) among labelled rows (label not NULL).
  Fails when there is no labelled row at all, or the rate is outside [min_value, max_value]. The
  measured rate is reported by rl_pipeline (dbt tasks' summary) and in docs/data/PIPELINE.md.
#}
{% test label_rate_between(model, column_name, min_value, max_value) %}

with stats as (
    select
        count({{ adapter.quote(column_name) }}) as n_labelled,
        avg({{ adapter.quote(column_name) }}::double) as label_rate
    from {{ model }}
)

select n_labelled, label_rate, {{ min_value }} as min_value, {{ max_value }} as max_value
from stats
where n_labelled = 0
   or label_rate < {{ min_value }}
   or label_rate > {{ max_value }}

{% endtest %}
