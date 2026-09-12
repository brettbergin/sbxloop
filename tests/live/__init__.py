"""Live forges for field verification (#1016) and the conformance suite.

``docker-compose.yml`` runs GitLab CE and Gitea on this machine; ``harness``
brings them up behind a throwaway CA, ``seed_gitlab`` and ``seed_gitea``
arrange the repository shape the conformance suite's ``Seeds`` and the
field questions need and write every resolved name, and the token values,
to ``.state/live.env`` (git-ignored). Nothing here reaches a forge unless
that file, or the equivalent environment, is supplied; see ``README.md``.
"""
