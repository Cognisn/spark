"""Identity, permissions and audit in server mode, built on warden.

warden (``cognisn-warden``) is PostgreSQL only, so everything here serves
server mode; personal mode keeps its single local user.

* :mod:`spark.identity.migrations`: warden's migration chain, run by
  ``spark migrate`` after SPARK's and checked at start-up.
* :mod:`spark.identity.db`: warden's database handle over SPARK's psycopg pool,
  so warden's writes (audit events) can join SPARK's transactions.

SPARK user guids are mapped onto warden users by
:mod:`spark.database.user_identities`.
"""
