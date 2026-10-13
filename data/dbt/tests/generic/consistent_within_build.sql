{#
  Build-level consistency: within each group (a build), every listed column has one value.

  null_aware=true  (default) a NULL mixed with a value also counts as a disagreement;
  null_aware=false only non-NULL values must agree (for columns with known cast-failure NULLs).
  row_filter       optional SQL predicate restricting the rows compared (e.g. listed rows only).

  Returns one row per offending group, listing the disagreeing columns.
#}
{% test consistent_within_build(model, columns, group_by='tr_build_id', row_filter=none, null_aware=true) %}

with rows_in_scope as (
    select * from {{ model }}
    {% if row_filter %} where {{ row_filter }} {% endif %}
),

per_group as (
    select
        {{ group_by }} as group_key,
        list_filter([
            {%- for col in columns %}
            case when
                count(distinct {{ adapter.quote(col) }}) > 1
                {%- if null_aware %}
                or count({{ adapter.quote(col) }}) between 1 and count(*) - 1
                {%- endif %}
            then '{{ col }}' end{{ ',' if not loop.last }}
            {%- endfor %}
        ], x -> x is not null) as disagreeing_columns
    from rows_in_scope
    group by {{ group_by }}
)

select group_key, disagreeing_columns
from per_group
where len(disagreeing_columns) > 0

{% endtest %}
