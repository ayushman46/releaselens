{#
  Leakage guard (spec §8.2 "History rule", ADR-0006): every history input of a row finished
  strictly before that row's prediction moment. The audit column is rl_core's
  max_history_input_finished_at (epoch seconds of the latest finish time among the past builds
  that fed the row's history features; NULL when no past build counted).

  Returns every row whose audit value is >= its started_at.
#}
{% test finished_before_started(model, column_name, started_column='started_at', key_column='build_id') %}

select
    {{ adapter.quote(key_column) }} as key,
    {{ adapter.quote(column_name) }} as max_history_input_finished_at,
    epoch_us({{ adapter.quote(started_column) }}) / 1e6 as started_at_epoch_s
from {{ model }}
where {{ adapter.quote(column_name) }} is not null
  and {{ adapter.quote(column_name) }} >= epoch_us({{ adapter.quote(started_column) }}) / 1e6

{% endtest %}
