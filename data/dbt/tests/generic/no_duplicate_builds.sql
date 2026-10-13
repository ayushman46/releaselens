{#
  One row per build: the column is non-NULL and unique, and (optionally) the model holds exactly
  the build ids of a reference relation (`compare_to`, filtered by `compare_filter`), so no build
  was dropped or invented between layers.

  Returns one row per problem: duplicate, null, missing (in the reference only) or extra.
#}
{% test no_duplicate_builds(model, column_name, compare_to=none, compare_column='build_id', compare_filter=none) %}

with model_ids as (
    select {{ adapter.quote(column_name) }} as build_id from {{ model }}
),

problems as (
    select build_id, 'duplicate' as problem, count(*) as n
    from model_ids
    where build_id is not null
    group by build_id
    having count(*) > 1

    union all

    select null as build_id, 'null' as problem, count(*) as n
    from model_ids
    where build_id is null
    having count(*) > 0

    {% if compare_to %}
    union all

    (
        select {{ adapter.quote(compare_column) }} as build_id, 'missing' as problem, 1 as n
        from {{ compare_to }}
        {% if compare_filter %} where {{ compare_filter }} {% endif %}
        except
        select build_id, 'missing' as problem, 1 as n from model_ids
    )

    union all

    (
        select build_id, 'extra' as problem, 1 as n from model_ids
        except
        select {{ adapter.quote(compare_column) }} as build_id, 'extra' as problem, 1 as n
        from {{ compare_to }}
        {% if compare_filter %} where {{ compare_filter }} {% endif %}
    )
    {% endif %}
)

select * from problems

{% endtest %}
