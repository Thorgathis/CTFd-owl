import datetime

from CTFd.models import db, Challenges


class DynamicCheckChallenge(Challenges):
    __mapper_args__ = {"polymorphic_identity": "dynamic_check_docker"}
    id = db.Column(None, db.ForeignKey("challenges.id",
                                       ondelete="CASCADE"), primary_key=True)

    # deployments settings
    deployment = db.Column(db.String(32))
    dirname = db.Column(db.String(80))
    flag_type = db.Column(db.Text, default="dynamic")

    initial = db.Column(db.Integer, default=0)
    minimum = db.Column(db.Integer, default=0)
    decay = db.Column(db.Integer, default=0)

    # frp settings
    redirect_type = db.Column(db.Text, default="DIRECT")
    redirect_port = db.Column(db.Integer, default=80)
    instance_mode = db.Column(db.String(32), nullable=False, default="personal")

    def __init__(self, *args, **kwargs):
        super(DynamicCheckChallenge, self).__init__(**kwargs)
        self.initial = kwargs["value"]


class SharedDynamicCheckChallenge(DynamicCheckChallenge):
    __mapper_args__ = {"polymorphic_identity": "dynamic_check_docker_shared"}

    def __init__(self, *args, **kwargs):
        super(SharedDynamicCheckChallenge, self).__init__(**kwargs)
        self.instance_mode = "shared"


class MultiDynamicCheckChallenge(DynamicCheckChallenge):
    """A dynamic docker challenge whose single container backs several linked tasks.

    One "group" is anchored by a parent challenge (``parent_id is None``) that owns
    the deployment settings (``dirname``, compose, timing). Each task in the group
    (parent included) consumes its own dynamic flag, addressed by ``flag_index``:
    the container is launched once with ``FLAG1..FLAGN`` env vars and every task
    accepts the flag matching its ``flag_index``.

    Uses single-table inheritance on the ``dynamic_check_challenge`` table, so the
    extra columns are added best-effort by ``DBUtils.ensure_schema`` for existing
    installs.
    """

    __mapper_args__ = {"polymorphic_identity": "dynamic_check_docker_multi"}

    # NULL for the group anchor (parent); the parent's challenge id for child tasks.
    parent_id = db.Column(db.Integer, nullable=True)
    # 1-based index of the flag this task consumes (FLAG<flag_index>).
    flag_index = db.Column(db.Integer, default=1)
    # Number of tasks in the group. Only meaningful on the parent; used at create time.
    flag_count = db.Column(db.Integer, default=1)


class OwlConfigs(db.Model):
    key = db.Column(db.String(length=128), primary_key=True)
    value = db.Column(db.Text)

    def __init__(self, key, value):
        self.key = key
        self.value = value


class OwlContainers(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), default="")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id"))
    docker_id = db.Column(db.String(32))
    ip = db.Column(db.String(32))
    port = db.Column(db.Integer)
    start_time = db.Column(db.DateTime, nullable=False, default=datetime.datetime.now(datetime.timezone.utc))
    renew_count = db.Column(db.Integer, nullable=False, default=0)
    flag = db.Column(db.String(128), nullable=False)
    instance_mode = db.Column(db.String(32), nullable=False, default="personal")
    idle_since = db.Column(db.DateTime, nullable=True)

    # Extra owl.label.* values for forward-compatible UI/logic (stored as JSON string).
    # Kept as VARCHAR for portable defaults across SQLite/MySQL/Postgres.
    labels = db.Column(db.String(2048), default="{}")

    # Relationships
    user = db.relationship("Users", foreign_keys="OwlContainers.user_id", lazy="select")
    challenge = db.relationship(
        "Challenges", foreign_keys="OwlContainers.challenge_id", lazy="select"
    )

    def __init__(self, *args, **kwargs):
        super(OwlContainers, self).__init__(**kwargs)


class OwlLaunchLocks(db.Model):
    """A short-lived lock to prevent concurrent instance launches per owner."""

    user_id = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, nullable=True)
    start_time = db.Column(db.DateTime, nullable=False, default=datetime.datetime.now(datetime.timezone.utc))

    def __init__(self, *args, **kwargs):
        super(OwlLaunchLocks, self).__init__(**kwargs)


class OwlSharedSessions(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id"), primary_key=True)
    last_seen = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.datetime.now(datetime.timezone.utc),
    )

    user = db.relationship("Users", foreign_keys="OwlSharedSessions.user_id", lazy="select")
    challenge = db.relationship(
        "Challenges", foreign_keys="OwlSharedSessions.challenge_id", lazy="select"
    )

    def __init__(self, *args, **kwargs):
        super(OwlSharedSessions, self).__init__(**kwargs)
