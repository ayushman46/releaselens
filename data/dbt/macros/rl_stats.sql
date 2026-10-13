{#
  95% Wilson score interval bounds for a binomial proportion x / n (z = 1.959963984540054), the
  same formula and clamping as rl_core.stats.wilson_interval: the interval always contains the
  estimate (low = 0 when x = 0, high = 1 when x = n, floating-point overshoot clamped). NULL when
  n = 0. x and n are integer SQL expressions (aggregates are fine: everything is inlined).
#}
{% macro _wilson_p(x, n) -%}
    (({{ x }})::double / ({{ n }}))
{%- endmacro %}

{% macro _wilson_centre(x, n, z) -%}
    (({{ _wilson_p(x, n) }} + {{ z }} * {{ z }} / (2.0 * ({{ n }})))
        / (1 + {{ z }} * {{ z }} / ({{ n }})))
{%- endmacro %}

{% macro _wilson_half(x, n, z) -%}
    ({{ z }} * sqrt({{ _wilson_p(x, n) }} * (1 - {{ _wilson_p(x, n) }}) / ({{ n }})
        + {{ z }} * {{ z }} / (4.0 * ({{ n }}) * ({{ n }})))
        / (1 + {{ z }} * {{ z }} / ({{ n }})))
{%- endmacro %}

{% macro wilson_low(x, n, z=1.959963984540054) -%}
    case
        when ({{ n }}) <= 0 then null
        when ({{ x }}) = 0 then 0.0
        else least({{ _wilson_p(x, n) }},
                   greatest(0.0, {{ _wilson_centre(x, n, z) }} - {{ _wilson_half(x, n, z) }}))
    end
{%- endmacro %}

{% macro wilson_high(x, n, z=1.959963984540054) -%}
    case
        when ({{ n }}) <= 0 then null
        when ({{ x }}) = ({{ n }}) then 1.0
        else greatest({{ _wilson_p(x, n) }},
                      least(1.0, {{ _wilson_centre(x, n, z) }} + {{ _wilson_half(x, n, z) }}))
    end
{%- endmacro %}
