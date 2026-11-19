"""Headless first run of a Spark server: ``spark bootstrap`` and the waits that follow it.

* :mod:`spark.bootstrap.settings`: the bootstrap settings, checked all at once;
* :mod:`spark.bootstrap.run`: the command's steps, in one advisory lock;
* :mod:`spark.bootstrap.wait`: web and worker waiting for bootstrap to finish;
* :mod:`spark.bootstrap.cli`: ``spark bootstrap [--resend-invitation]``.

Server mode only; personal mode has no warden and never bootstraps.
"""
