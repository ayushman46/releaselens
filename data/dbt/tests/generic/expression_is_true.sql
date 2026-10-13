{# Row-level invariant: returns the rows for which the SQL boolean expression is not true. #}
{% test expression_is_true(model, expression, column_name=none) %}

select * from {{ model }} where not coalesce(({{ expression }}), false)

{% endtest %}
