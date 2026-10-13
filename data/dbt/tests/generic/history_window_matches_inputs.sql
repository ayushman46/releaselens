{#
  Independent leakage oracle for rl_core's history features (spec §8.2 "History rule", ADR-0006).

  finished_before_started checks rl_core's audit column against started_at, but rl_core computes
  that column itself, separately from the features: a history window that admitted a build which
  finished at or after the prediction moment would still report a clean audit value. This test
  recomputes the window from the model's own inputs in plain SQL instead. Per row it counts the
  builds of the same group (project) that had an outcome (`outcome_column` not NULL) and finished
  STRICTLY before the row's started_at, and takes the latest such finish time (one sweep over
  finish and query events per group; at equal timestamps the query sorts first, so a build that
  finished exactly at the prediction moment is not counted). rl_core's `history_size` feature and
  its audit column must equal them exactly. Every rl_core history feature indexes this same
  window, so an off-by-one or a leaked later build fails here.

  A test oracle, not a feature source: the features themselves come from rl_core only. Epoch
  seconds are epoch_us / 1e6 on both sides (rl_core: float(epoch_us) / 1e6), so equality is exact.

  Returns one row per build whose window or audit value differs.
#}
{% test history_window_matches_inputs(model, inputs, column_name='max_history_input_finished_at', size_column='history_size', key_column='build_id', group_column='project', started_column='started_at', finished_column='finished_at', outcome_column='failed') %}

with eligible as (
    select
        {{ adapter.quote(group_column) }} as grp,
        epoch_us({{ adapter.quote(finished_column) }}) / 1e6 as ts
    from {{ inputs }}
    where {{ adapter.quote(outcome_column) }} is not null
      and {{ adapter.quote(finished_column) }} is not null
),

queries as (
    select
        {{ adapter.quote(key_column) }} as key,
        {{ adapter.quote(group_column) }} as grp,
        epoch_us({{ adapter.quote(started_column) }}) / 1e6 as ts,
        {{ adapter.quote(size_column) }}::double as history_size,
        {{ adapter.quote(column_name) }}::double as audit
    from {{ model }}
),

events as (
    select grp, ts, 1 as is_finish, null as key, null::double as history_size, null::double as audit
    from eligible
    union all
    select grp, ts, 0 as is_finish, key, history_size, audit
    from queries
),

swept as (
    select
        *,
        sum(is_finish) over w as n_finished_before,
        max(case when is_finish = 1 then ts end) over w as last_finished_before
    from events
    window w as (
        partition by grp order by ts, is_finish rows between unbounded preceding and current row
    )
)

select key, ts as started_at_epoch_s, history_size, n_finished_before, audit, last_finished_before
from swept
where is_finish = 0
  and (history_size is distinct from n_finished_before::double
       or audit is distinct from last_finished_before)

{% endtest %}
