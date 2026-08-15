import math

from flask import Blueprint, current_app, Request

from CTFd.models import (
    db,
    Solves,
    Fails,
    Flags,
    Challenges,
    ChallengeFiles,
    Tags,
    Hints,
    Users,
    Notifications,
)
from CTFd.plugins.challenges import BaseChallenge
from CTFd.plugins.flags import get_flag_class, FlagException
from CTFd.utils.user import get_current_user as ctfd_get_current_user
from CTFd.utils import get_config
from CTFd.utils.modes import get_model
from CTFd.utils.uploads import delete_file
from CTFd.utils.user import get_ip
from .utils.db_utils import DBUtils
from .models import (
    DynamicCheckChallenge,
    MultiDynamicCheckChallenge,
    OwlContainers,
    OwlSharedSessions,
    SharedDynamicCheckChallenge,
)


SHARED_CHALLENGE_TYPE_ID = "dynamic_check_docker_shared"
MULTI_CHALLENGE_TYPE_ID = "dynamic_check_docker_multi"


class BaseDynamicCheckValueChallenge(BaseChallenge):
    blueprint = Blueprint(
        "ctfd-owl-challenge",
        __name__,
        template_folder="templates",
        static_folder="assets",
        url_prefix="/plugins/ctfd-owl"
    )
    challenge_model = DynamicCheckChallenge
    instance_mode = "personal"

    @classmethod
    def read(cls, challenge):
        challenge = cls.challenge_model.query.filter_by(id=challenge.id).first()
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "initial": challenge.initial,
            "decay": challenge.decay,
            "minimum": challenge.minimum,
            "description": challenge.description,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "instance_mode": cls.instance_mode,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }
        return data

    @classmethod
    def update(cls, challenge: challenge_model, request: Request):
        data = request.form or request.get_json()
        for attr, value in data.items():
            if attr in ("initial", "minimum", "decay"):
                value = float(value)
            setattr(challenge, attr, value)

        if hasattr(challenge, "instance_mode"):
            challenge.instance_mode = cls.instance_mode

        model = get_model()

        solve_count = (
            Solves.query.join(model, Solves.account_id == model.id)
            .filter(
                Solves.challenge_id == challenge.id,
                model.hidden is False,
                model.banned is False,
            )
            .count()
        )

        value = (
            ((challenge.minimum - challenge.initial) / (challenge.decay ** 2))
            * (solve_count ** 2)
        ) + challenge.initial

        value = math.ceil(value)

        if value < challenge.minimum:
            value = challenge.minimum

        challenge.value = value

        db.session.commit()
        return challenge

    @classmethod
    def delete(cls, challenge):
        Fails.query.filter_by(challenge_id=challenge.id).delete()
        Solves.query.filter_by(challenge_id=challenge.id).delete()
        Flags.query.filter_by(challenge_id=challenge.id).delete()
        OwlContainers.query.filter_by(challenge_id=challenge.id).delete()
        OwlSharedSessions.query.filter_by(challenge_id=challenge.id).delete()
        files = ChallengeFiles.query.filter_by(challenge_id=challenge.id).all()
        for f in files:
            delete_file(f.id)
        ChallengeFiles.query.filter_by(challenge_id=challenge.id).delete()
        Tags.query.filter_by(challenge_id=challenge.id).delete()
        Hints.query.filter_by(challenge_id=challenge.id).delete()
        cls.challenge_model.query.filter_by(id=challenge.id).delete()
        Challenges.query.filter_by(id=challenge.id).delete()
        db.session.commit()

    @classmethod
    def attempt(cls, challenge, request):
        chal = cls.challenge_model.query.filter_by(id=challenge.id).first()
        data = request.form or request.get_json()
        submission = data["submission"].strip()
        user = ctfd_get_current_user()
        user_id = user.id

        if chal.flag_type == 'static':
            flags: list[Flags] = Flags.query.filter_by(challenge_id=challenge.id).all()
            for flag in flags:
                try:
                    if get_flag_class(flag.type).compare(flag, submission):
                        return True, "Correct"
                except FlagException as e:
                    return False, str(e)
            return False, "Incorrect"

        if cls.instance_mode == "shared":
            shared_rows = DBUtils.get_shared_container_rows(challenge_id=challenge.id)
            shared_flag_row = shared_rows[0] if shared_rows else None

            has_access = DBUtils.has_active_shared_session(user_id=user_id, challenge_id=challenge.id)
            if shared_flag_row and submission == shared_flag_row.flag:
                if has_access and DBUtils.is_container_alive(shared_flag_row):
                    return True, "Correct"
                return False, "Please solve it during the shared instance is running"

            if has_access and shared_flag_row and DBUtils.is_container_alive(shared_flag_row):
                return False, "Incorrect"
            return False, "Please solve it during the shared instance is running"

        container = OwlContainers.query.filter_by(user_id=user_id, challenge_id=challenge.id).first()
        subflag = OwlContainers.query.filter_by(flag=submission).first()

        if subflag:
            if int(subflag.challenge_id) != int(challenge.id):
                return False, "Incorrect Challenge"
            try:
                fflag = container.flag
            except Exception:
                fflag = ""
            if fflag == submission:
                return True, "Correct"
            else:
                flaguser = Users.query.filter_by(id=user_id).first()
                subuser = Users.query.filter_by(id=subflag.user_id).first()

                if (get_config("user_mode") == "teams" and flaguser and subuser
                    and getattr(flaguser, "team_id", None) and flaguser.team_id == getattr(subuser, "team_id", None)):
                    return True, "Correct"

                if flaguser.name == subuser.name:
                    return False, "Incorrect Challenge"
                else:
                    if flaguser.type == "admin":
                        return False, "Admin Test Other's Flag"
                    message = flaguser.name + " Submitted " + subuser.name + "'s Flag."
                    db.session.add(Notifications(title="Cheat Found", content=message))
                    flaguser.banned = True
                    db.session.commit()
                    messages = {"title": "Cheat Found", "content": message, "type": "background", "sound": True}
                    current_app.events_manager.publish(data=messages, type="notification")
                    return False, "Cheated"
        elif container:
            return False, "Incorrect"
        else:
            return False, "Please solve it during the container is running"

    @classmethod
    def solve(cls, user, team, challenge, request):
        chal = cls.challenge_model.query.filter_by(id=challenge.id).first()
        data = request.form or request.get_json()
        submission = data["submission"].strip()

        model = get_model()

        solve = Solves(
            user_id=user.id,
            team_id=team.id if team else None,
            challenge_id=challenge.id,
            ip=get_ip(req=request),
            provided=submission,
        )
        db.session.add(solve)

        solve_count = (
            Solves.query.join(model, Solves.account_id == model.id)
            .filter(
                Solves.challenge_id == challenge.id,
                model.hidden is False,
                model.banned is False,
            )
            .count()
        )

        solve_count -= 1

        value = (
            ((chal.minimum - chal.initial) / (chal.decay ** 2)) * (solve_count ** 2)
        ) + chal.initial

        value = math.ceil(value)

        if value < chal.minimum:
            value = chal.minimum
        chal.value = value

        db.session.commit()


class DynamicCheckValueChallenge(BaseDynamicCheckValueChallenge):
    id = "dynamic_check_docker"
    name = "dynamic_check_docker"
    challenge_model = DynamicCheckChallenge
    instance_mode = "personal"


class SharedDynamicCheckValueChallenge(BaseDynamicCheckValueChallenge):
    id = SHARED_CHALLENGE_TYPE_ID
    name = SHARED_CHALLENGE_TYPE_ID
    challenge_model = SharedDynamicCheckChallenge
    instance_mode = "shared"


class MultiDynamicCheckValueChallenge(BaseDynamicCheckValueChallenge):
    """A group of linked dynamic tasks backed by a single container.

    The parent (group anchor) owns the deployment; creating it auto-creates the
    remaining child tasks. Each task consumes its own dynamic flag by ``flag_index``.
    """

    id = MULTI_CHALLENGE_TYPE_ID
    name = MULTI_CHALLENGE_TYPE_ID
    challenge_model = MultiDynamicCheckChallenge
    instance_mode = "personal"

    @classmethod
    def create(cls, request):
        data = request.form or request.get_json()

        def _to_int(value, default):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        # Only the parent carries flag_count / auto-creates children.
        flag_count = max(1, _to_int(data.get("flag_count"), 1))

        # Build the parent from the submitted fields (drop our control-only keys).
        parent_kwargs = {k: v for k, v in data.items() if k not in ("flag_count",)}
        parent = cls.challenge_model(**parent_kwargs)
        parent.parent_id = None
        parent.flag_index = 1
        parent.flag_count = flag_count
        # Name tasks as "Name [n/z]" (only when the group has more than one task).
        base_name = parent.name
        if flag_count > 1:
            parent.name = f"{base_name} [1/{flag_count}]"
        db.session.add(parent)
        db.session.commit()

        # Auto-create child tasks 2..N sharing the parent's settings/deployment group.
        for idx in range(2, flag_count + 1):
            child = cls.challenge_model(
                name=f"{base_name} [{idx}/{flag_count}]",
                description=parent.description,
                value=parent.value,
                category=parent.category,
                # Inherit the parent's visibility state (e.g. visible/hidden).
                state=parent.state,
                type=MULTI_CHALLENGE_TYPE_ID,
            )
            child.initial = parent.initial
            child.minimum = parent.minimum
            child.decay = parent.decay
            child.flag_type = parent.flag_type
            # Children have no compose of their own; they ride the parent's container.
            child.dirname = None
            child.deployment = parent.deployment
            child.instance_mode = "personal"
            child.parent_id = parent.id
            child.flag_index = idx
            child.flag_count = 1
            db.session.add(child)
        db.session.commit()

        return parent

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json()
        data = dict(data)

        # Normalize multitask control fields (empty string -> None, cast ints).
        if "parent_id" in data:
            raw = str(data.get("parent_id") or "").strip()
            data["parent_id"] = int(raw) if raw else None
        for key in ("flag_index", "flag_count"):
            if key in data and str(data.get(key) or "").strip() != "":
                try:
                    data[key] = int(data[key])
                except (TypeError, ValueError):
                    data.pop(key)

        for attr, value in data.items():
            if attr in ("initial", "minimum", "decay"):
                value = float(value)
            setattr(challenge, attr, value)

        challenge.instance_mode = cls.instance_mode

        model = get_model()
        solve_count = (
            Solves.query.join(model, Solves.account_id == model.id)
            .filter(
                Solves.challenge_id == challenge.id,
                model.hidden is False,
                model.banned is False,
            )
            .count()
        )
        value = (
            ((challenge.minimum - challenge.initial) / (challenge.decay ** 2))
            * (solve_count ** 2)
        ) + challenge.initial
        value = math.ceil(value)
        if value < challenge.minimum:
            value = challenge.minimum
        challenge.value = value

        db.session.commit()

        # When the parent's visibility changes, mirror it onto the child tasks.
        if getattr(challenge, "parent_id", None) is None:
            children = cls.challenge_model.query.filter_by(parent_id=challenge.id).all()
            changed = False
            for child in children:
                if int(child.id) != int(challenge.id) and child.state != challenge.state:
                    child.state = challenge.state
                    changed = True
            if changed:
                db.session.commit()

        return challenge

    @classmethod
    def read(cls, challenge):
        challenge = cls.challenge_model.query.filter_by(id=challenge.id).first()
        data = super().read(challenge)
        data["parent_id"] = challenge.parent_id
        data["flag_index"] = challenge.flag_index
        data["flag_count"] = challenge.flag_count
        return data

    @classmethod
    def delete(cls, challenge):
        # Deleting the parent removes the whole group (children first).
        chal = cls.challenge_model.query.filter_by(id=challenge.id).first()
        if chal is not None and getattr(chal, "parent_id", None) is None:
            children = cls.challenge_model.query.filter_by(parent_id=chal.id).all()
            for child in children:
                if int(child.id) != int(chal.id):
                    super().delete(child)
        super().delete(challenge)
