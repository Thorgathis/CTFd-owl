import datetime

from CTFd.models import db
from sqlalchemy import distinct
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect
from sqlalchemy.schema import CreateColumn, DDL

from ..models import (
    DynamicCheckChallenge,
    MultiDynamicCheckChallenge,
    OwlConfigs,
    OwlContainers,
    OwlLaunchLocks,
    OwlSharedSessions,
)


class DBUtils:
    @staticmethod
    def utcnow():
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    @staticmethod
    def get_docker_timeout(configs=None):
        cfg = configs or DBUtils.get_all_configs()
        return int(cfg.get("docker_timeout", "3600") or "3600")

    @staticmethod
    def get_shared_idle_timeout(configs=None):
        cfg = configs or DBUtils.get_all_configs()
        return int(cfg.get("shared_instance_grace_timeout", "1800") or "1800")

    @staticmethod
    def _ensure_column(table, column_name):
        inspector = inspect(db.engine)
        cols = {c["name"] for c in inspector.get_columns(table.name)}
        if column_name in cols:
            return False

        # Build the column definition from SQLAlchemy metadata instead of hardcoding SQL types/defaults.
        column_ddl = str(CreateColumn(table.c[column_name].copy()).compile(dialect=db.engine.dialect))
        table_name = db.engine.dialect.identifier_preparer.quote(table.name)
        statement = DDL(f"ALTER TABLE {table_name} ADD COLUMN {column_ddl}")
        with db.engine.begin() as conn:
            conn.execute(statement)
        return True

    @staticmethod
    def ensure_schema():
        """Ensure plugin tables have expected columns.

        Note: `db.create_all()` does not add new columns to existing tables.
        This performs a small, best-effort migration for backward compatibility.
        """
        try:
            DBUtils._ensure_column(OwlContainers.__table__, "labels")
            DBUtils._ensure_column(OwlContainers.__table__, "instance_mode")
            DBUtils._ensure_column(OwlContainers.__table__, "idle_since")

            with db.engine.begin() as conn:
                conn.execute(
                    OwlContainers.__table__.update()
                    .where(OwlContainers.__table__.c.labels.is_(None))
                    .values(labels="{}")
                )
                conn.execute(
                    OwlContainers.__table__.update()
                    .where(OwlContainers.__table__.c.instance_mode.is_(None))
                    .values(instance_mode="personal")
                )
        except Exception:
            pass

        try:
            DBUtils._ensure_column(DynamicCheckChallenge.__table__, "instance_mode")
            with db.engine.begin() as conn:
                conn.execute(
                    DynamicCheckChallenge.__table__.update()
                    .where(DynamicCheckChallenge.__table__.c.instance_mode.is_(None))
                    .values(instance_mode="personal")
                )
        except Exception:
            pass

        # Multitask columns live on the same (single-table inheritance) table.
        try:
            DBUtils._ensure_column(DynamicCheckChallenge.__table__, "parent_id")
            DBUtils._ensure_column(DynamicCheckChallenge.__table__, "flag_index")
            DBUtils._ensure_column(DynamicCheckChallenge.__table__, "flag_count")
        except Exception:
            pass

    @staticmethod
    def get_all_configs():
        configs = OwlConfigs.query.all()
        result = {}

        for c in configs:
            result[str(c.key)] = str(c.value)

        return result

    @staticmethod
    def save_all_configs(configs):
        for c in configs:
            q = db.session.query(OwlConfigs)
            q = q.filter(OwlConfigs.key == c[0])
            record = q.one_or_none()

            if record:
                record.value = c[1]
                db.session.commit()
            else:
                config = OwlConfigs(key=c[0], value=c[1])
                db.session.add(config)
                db.session.commit()
        db.session.close()

    @staticmethod
    def new_container(
        user_id,
        challenge_id,
        flag,
        docker_id,
        port=0,
        ip="",
        name="",
        labels="{}",
        instance_mode="personal",
    ):
        """Create a new container DB record."""
        container = OwlContainers(user_id=user_id, challenge_id=challenge_id, flag=flag, docker_id=docker_id, port=port,
                                  ip=ip, name=name,
                                  labels=labels,
                                  instance_mode=instance_mode,
                                  start_time=datetime.datetime.now(datetime.timezone.utc))
        db.session.add(container)
        db.session.commit()
        db.session.close()
        return str(docker_id)

    @staticmethod
    def get_current_containers(user_id):
        """Get all containers for a owner user_id."""
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        records = q.all()
        if len(records) == 0:
            return None

        return records

    @staticmethod
    def get_current_containers_for_challenge(user_id, challenge_id):
        """Get all containers (service rows) for a given owner and challenge."""
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        q = q.filter(OwlContainers.challenge_id == challenge_id)
        records = q.all()
        if len(records) == 0:
            return None
        return records

    @staticmethod
    def get_container_by_port(port):
        """Get container for a port."""
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.port == port)
        records = q.all()
        if len(records) == 0:
            return None

        return records[0]

    @staticmethod
    def remove_current_container(user_id):
        """Remove container for a owner user_id."""
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        # records = q.all()
        # for r in records:
        #     pass

        q.delete()
        db.session.commit()
        db.session.close()

    @staticmethod
    def remove_current_container_for_challenge(user_id, challenge_id):
        """Remove container rows for a given owner and challenge."""
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        q = q.filter(OwlContainers.challenge_id == challenge_id)
        q.delete()
        db.session.commit()
        db.session.close()

    @staticmethod
    def remove_current_container_for_challenges(user_id, challenge_ids):
        """Remove container rows for a given owner across several challenge ids (a multitask group)."""
        if not challenge_ids:
            return
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        q = q.filter(OwlContainers.challenge_id.in_(list(challenge_ids)))
        q.delete(synchronize_session=False)
        db.session.commit()
        db.session.close()

    @staticmethod
    def get_group_containers(user_id, challenge_ids):
        """Get all container rows for a given owner across a multitask group's challenge ids."""
        if not challenge_ids:
            return None
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        q = q.filter(OwlContainers.challenge_id.in_(list(challenge_ids)))
        records = q.all()
        if len(records) == 0:
            return None
        return records

    @staticmethod
    def renew_current_container_for_challenges(user_id, challenge_ids):
        """Extend container lifetime across a multitask group and increment renew_count."""
        if not challenge_ids:
            return
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        q = q.filter(OwlContainers.challenge_id.in_(list(challenge_ids)))
        records = q.all()
        if len(records) == 0:
            return

        configs = DBUtils.get_all_configs()
        timeout = DBUtils.get_docker_timeout(configs)

        for r in records:
            r.start_time = r.start_time + datetime.timedelta(seconds=timeout)
            if r.start_time > DBUtils.utcnow():
                r.start_time = DBUtils.utcnow()
            r.renew_count += 1
        db.session.commit()
        db.session.close()

    @staticmethod
    def renew_current_container(user_id):
        """Extend container lifetime and increment renew_count."""
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        # q = q.filter(OwlContainers.challenge_id == challenge_id)
        records = q.all()
        if len(records) == 0:
            return

        configs = DBUtils.get_all_configs()
        timeout = DBUtils.get_docker_timeout(configs)

        for r in records:
            r.start_time = r.start_time + datetime.timedelta(seconds=timeout)

            if r.start_time > DBUtils.utcnow():
                r.start_time = DBUtils.utcnow()

            r.renew_count += 1
        db.session.commit()
        db.session.close()

    @staticmethod
    def renew_current_container_for_challenge(user_id, challenge_id):
        """Extend container lifetime for a specific challenge and increment renew_count."""
        q = db.session.query(OwlContainers)
        q = q.filter(OwlContainers.user_id == user_id)
        q = q.filter(OwlContainers.challenge_id == challenge_id)
        records = q.all()
        if len(records) == 0:
            return

        configs = DBUtils.get_all_configs()
        timeout = DBUtils.get_docker_timeout(configs)

        for r in records:
            r.start_time = r.start_time + datetime.timedelta(seconds=timeout)

            if r.start_time > DBUtils.utcnow():
                r.start_time = DBUtils.utcnow()

            r.renew_count += 1
        db.session.commit()
        db.session.close()

    @staticmethod
    def get_alive_instance_count_for_user(user_id):
        """Count alive instances for an owner, by distinct docker_id (one container = one instance).

        Using docker_id (rather than challenge_id) makes a multitask group, whose per-task
        rows share a single container, count as one instance.
        """
        configs = DBUtils.get_all_configs()
        timeout = DBUtils.get_docker_timeout(configs)
        threshold = DBUtils.utcnow() - datetime.timedelta(seconds=timeout)

        q = db.session.query(distinct(OwlContainers.docker_id))
        q = q.filter(OwlContainers.user_id == user_id)
        q = q.filter(OwlContainers.instance_mode != "shared")
        q = q.filter(OwlContainers.start_time >= threshold)
        # SQLAlchemy emits SELECT DISTINCT; count() works across backends.
        return q.count()

    @staticmethod
    def get_alive_instance_count_for_team(user_ids: list[int]):
        """Count alive instances for a team, by distinct (user_id, docker_id)."""
        if not user_ids:
            return 0

        configs = DBUtils.get_all_configs()
        timeout = DBUtils.get_docker_timeout(configs)
        threshold = DBUtils.utcnow() - datetime.timedelta(seconds=timeout)

        q = db.session.query(OwlContainers.user_id, OwlContainers.docker_id).distinct()
        q = q.filter(OwlContainers.user_id.in_(user_ids))
        q = q.filter(OwlContainers.instance_mode != "shared")
        q = q.filter(OwlContainers.start_time >= threshold)
        return q.count()

    @staticmethod
    def acquire_launch_lock(user_id, challenge_id=None, ttl_seconds=120):
        """Acquire a best-effort launch lock for a given owner."""
        now = DBUtils.utcnow()
        threshold = now - datetime.timedelta(seconds=int(ttl_seconds))

        try:
            # Remove expired lock if any.
            db.session.query(OwlLaunchLocks).filter(OwlLaunchLocks.start_time < threshold).delete()
            db.session.commit()

            lock = OwlLaunchLocks(user_id=user_id, challenge_id=challenge_id, start_time=now)
            db.session.add(lock)
            db.session.commit()
            return True
        except IntegrityError:
            db.session.rollback()
            return False
        finally:
            db.session.close()

    @staticmethod
    def release_launch_lock(user_id):
        try:
            db.session.query(OwlLaunchLocks).filter(OwlLaunchLocks.user_id == user_id).delete()
            db.session.commit()
        finally:
            db.session.close()

    @staticmethod
    def get_all_expired_container():
        """Get all expired containers."""
        configs = DBUtils.get_all_configs()
        rows = DBUtils.get_all_container()
        return [row for row in rows if DBUtils.is_container_expired(row, configs)]

    @staticmethod
    def get_all_alive_container():
        """Get all alive containers."""
        configs = DBUtils.get_all_configs()
        rows = DBUtils.get_all_container()
        return [row for row in rows if DBUtils.is_container_alive(row, configs)]

    @staticmethod
    def get_all_container():
        q = db.session.query(OwlContainers)
        return q.all()

    @staticmethod
    def get_all_container_count():
        q = db.session.query(OwlContainers)
        return q.count()

    @staticmethod
    def get_all_alive_container_page(page_start, page_end):
        rows = DBUtils.get_all_alive_container()
        rows.sort(key=lambda row: row.start_time, reverse=True)
        return rows[page_start:page_end]

    @staticmethod
    def get_all_alive_container_count():
        return len(DBUtils.get_all_alive_container())

    @staticmethod
    def get_shared_container_rows(challenge_id):
        rows = (
            db.session.query(OwlContainers)
            .filter(OwlContainers.challenge_id == challenge_id)
            .filter(OwlContainers.instance_mode == "shared")
            .order_by(OwlContainers.start_time.desc(), OwlContainers.id.desc())
            .all()
        )
        if len(rows) == 0:
            return None

        docker_id = rows[0].docker_id
        selected = [row for row in rows if row.docker_id == docker_id]
        return selected or None

    @staticmethod
    def get_shared_session(user_id, challenge_id):
        return (
            db.session.query(OwlSharedSessions)
            .filter(OwlSharedSessions.user_id == user_id)
            .filter(OwlSharedSessions.challenge_id == challenge_id)
            .first()
        )

    @staticmethod
    def touch_shared_session(user_id, challenge_id):
        now = DBUtils.utcnow()
        record = DBUtils.get_shared_session(user_id=user_id, challenge_id=challenge_id)
        if record is None:
            record = OwlSharedSessions(user_id=user_id, challenge_id=challenge_id, last_seen=now)
            db.session.add(record)
        else:
            record.last_seen = now
        db.session.commit()
        db.session.close()
        return True

    @staticmethod
    def remove_shared_session(user_id, challenge_id):
        q = db.session.query(OwlSharedSessions)
        q = q.filter(OwlSharedSessions.user_id == user_id)
        q = q.filter(OwlSharedSessions.challenge_id == challenge_id)
        removed = q.delete()
        db.session.commit()
        db.session.close()
        return removed > 0

    @staticmethod
    def remove_shared_sessions_for_challenge(challenge_id):
        q = db.session.query(OwlSharedSessions)
        q = q.filter(OwlSharedSessions.challenge_id == challenge_id)
        q.delete()
        db.session.commit()
        db.session.close()

    @staticmethod
    def get_active_shared_session_count(challenge_id, configs=None):
        timeout = DBUtils.get_docker_timeout(configs)
        threshold = DBUtils.utcnow() - datetime.timedelta(seconds=timeout)
        q = db.session.query(OwlSharedSessions)
        q = q.filter(OwlSharedSessions.challenge_id == challenge_id)
        q = q.filter(OwlSharedSessions.last_seen >= threshold)
        return q.count()

    @staticmethod
    def has_active_shared_session(user_id, challenge_id, configs=None):
        timeout = DBUtils.get_docker_timeout(configs)
        threshold = DBUtils.utcnow() - datetime.timedelta(seconds=timeout)
        q = db.session.query(OwlSharedSessions)
        q = q.filter(OwlSharedSessions.user_id == user_id)
        q = q.filter(OwlSharedSessions.challenge_id == challenge_id)
        q = q.filter(OwlSharedSessions.last_seen >= threshold)
        return q.first() is not None

    @staticmethod
    def get_active_shared_session_challenge_ids(user_id, configs=None):
        timeout = DBUtils.get_docker_timeout(configs)
        threshold = DBUtils.utcnow() - datetime.timedelta(seconds=timeout)
        q = db.session.query(OwlSharedSessions.challenge_id)
        q = q.filter(OwlSharedSessions.user_id == user_id)
        q = q.filter(OwlSharedSessions.last_seen >= threshold)
        return [int(row.challenge_id) for row in q.all()]

    @staticmethod
    def cleanup_expired_shared_sessions(configs=None):
        timeout = DBUtils.get_docker_timeout(configs)
        threshold = DBUtils.utcnow() - datetime.timedelta(seconds=timeout)
        q = db.session.query(OwlSharedSessions)
        q = q.filter(OwlSharedSessions.last_seen < threshold)
        q.delete()
        db.session.commit()
        db.session.close()

    @staticmethod
    def touch_shared_container(challenge_id, increment_renew=False):
        rows = DBUtils.get_shared_container_rows(challenge_id=challenge_id)
        if not rows:
            return False

        now = DBUtils.utcnow()
        for row in rows:
            row.start_time = now
            row.idle_since = None
            if increment_renew:
                row.renew_count += 1
        db.session.commit()
        db.session.close()
        return True

    @staticmethod
    def mark_shared_container_idle(challenge_id, idle_since=None):
        rows = DBUtils.get_shared_container_rows(challenge_id=challenge_id)
        if not rows:
            return False

        idle_value = idle_since or DBUtils.utcnow()
        for row in rows:
            row.idle_since = idle_value
        db.session.commit()
        db.session.close()
        return True

    @staticmethod
    def get_container_remaining_time(container, configs=None):
        timeout = DBUtils.get_docker_timeout(configs)
        remaining = timeout - int((DBUtils.utcnow() - container.start_time).total_seconds())
        return max(0, remaining)

    @staticmethod
    def get_shared_idle_anchor(container, configs=None):
        timeout = DBUtils.get_docker_timeout(configs)
        if container.idle_since is not None:
            return container.idle_since
        return container.start_time + datetime.timedelta(seconds=timeout)

    @staticmethod
    def get_shared_idle_remaining(container, configs=None):
        shared_timeout = DBUtils.get_shared_idle_timeout(configs)
        anchor = DBUtils.get_shared_idle_anchor(container=container, configs=configs)
        remaining = shared_timeout - int((DBUtils.utcnow() - anchor).total_seconds())
        return max(0, remaining)

    @staticmethod
    def is_container_alive(container, configs=None):
        cfg = configs or DBUtils.get_all_configs()
        now = DBUtils.utcnow()
        instance_mode = str(getattr(container, "instance_mode", "personal") or "personal").lower()
        if instance_mode != "shared":
            timeout = DBUtils.get_docker_timeout(cfg)
            return container.start_time >= now - datetime.timedelta(seconds=timeout)

        active_sessions = DBUtils.get_active_shared_session_count(challenge_id=container.challenge_id, configs=cfg)
        if active_sessions > 0:
            timeout = DBUtils.get_docker_timeout(cfg)
            return container.start_time >= now - datetime.timedelta(seconds=timeout)

        shared_timeout = DBUtils.get_shared_idle_timeout(cfg)
        idle_anchor = DBUtils.get_shared_idle_anchor(container=container, configs=cfg)
        return idle_anchor >= now - datetime.timedelta(seconds=shared_timeout)

    @staticmethod
    def is_container_expired(container, configs=None):
        return not DBUtils.is_container_alive(container=container, configs=configs)
