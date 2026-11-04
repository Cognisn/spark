"""Usage metering and monthly token budgets (A14).

:mod:`spark.usage.meter` records every model call against its user;
:mod:`spark.usage.budgets` decides the limit that applies and refuses a new
request once a user has used their monthly budget.
"""
