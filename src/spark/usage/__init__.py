"""Usage metering and monthly credit allowances (A14, M6 to M10).

:mod:`spark.usage.meter` bills every model call to its user;
:mod:`spark.usage.credits` works out what a call costs in credits;
:mod:`spark.usage.budgets` decides the allowance that applies on a connection and
refuses a new request once a user has used it.
"""
