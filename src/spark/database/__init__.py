"""SPARK's repositories: async functions over the data layer in :mod:`spark.db`.

Each module (``conversations``, ``messages``, ``memories`` and so on) holds async
functions taking an :class:`spark.db.Executor` (a ``Database`` or a
``Connection``) as their first argument. Code on the event loop awaits them;
synchronous code on worker threads calls them through
:class:`spark.db.SyncDatabase`::

    sync_db.call(conversations.get_conversation, conversation_id, user_guid)

The schema is owned by the migration chain (:mod:`spark.db.migrate`).
"""
