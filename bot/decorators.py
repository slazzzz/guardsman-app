### PERMISSION CHECK DECORATORS ###

import time

from discord import Interaction, app_commands

from bot.config import ADMIN_USERS, MEMBER_ROLES, STAFF_ROLES, HELPER_ROLES


def is_staff():
    async def predicate(interaction: Interaction):
        user_roles = [role.id for role in interaction.user.roles]
        return any(r in STAFF_ROLES for r in user_roles)
    return app_commands.check(predicate)

def is_helper():
    async def predicate(interaction: Interaction):
        user_roles = [role.id for role in interaction.user.roles]
        return any(r in HELPER_ROLES for r in user_roles)
    return app_commands.check(predicate)

def is_member():
    async def predicate(interaction: Interaction):
        user_roles = [role.id for role in interaction.user.roles]
        return any(r in MEMBER_ROLES for r in user_roles)
    return app_commands.check(predicate)


def is_admin():
    async def predicate(interaction: Interaction):
        return interaction.user.id in ADMIN_USERS
    return app_commands.check(predicate)

def is_admin_or_staff():
    async def predicate(interaction: Interaction):
        if interaction.user.id in ADMIN_USERS:
            return True
        user_roles = [role.id for role in interaction.user.roles]
        return any(r in STAFF_ROLES for r in user_roles)
    return app_commands.check(predicate)

def is_admin_or_staff_or_helper():
    async def predicate(interaction: Interaction):
        if interaction.user.id in ADMIN_USERS:
            return True
        user_roles = [role.id for role in interaction.user.roles]
        if any(r in STAFF_ROLES for r in user_roles):
            return True
        if any(r in HELPER_ROLES for r in user_roles):
            return True
    return app_commands.check(predicate)

def is_allowed():
    async def predicate(interaction: Interaction):
        # Check user ID
        if interaction.user.id in ADMIN_USERS:
            return True

        # Check roles (staff OR regular division member)
        user_roles = [role.id for role in interaction.user.roles]
        if any(r in STAFF_ROLES for r in user_roles):
            return True
        if any(r in HELPER_ROLES for r in user_roles):
            return True
        if any(r in MEMBER_ROLES for r in user_roles):
            return True

        # Check permissions
        if interaction.user.guild_permissions.manage_guild:
            return True

        return False

    return app_commands.check(predicate)


def cooldown(seconds: float):
    """Per-user command cooldown, e.g. to stop /drill_create from being
    spammed. Admin/staff/helper are exempt - they're the trusted tier
    already, and they're exactly who might legitimately need to run the
    command back-to-back (e.g. covering several drills in a row, or fixing
    a botched drill by immediately recreating it).

    Kept in-process (not persisted to the DB) since the only thing at stake
    is spam, not access control - a bot restart clearing everyone's cooldown
    is a non-issue, unlike drill_banned_users or drill_vc_overrides, which
    do need to survive a restart.

    Raises app_commands.CommandOnCooldown on cooldown, same as the built-in
    app_commands.checks.cooldown() - see app.py's tree.error handler for how
    that's turned into a user-facing message.
    """
    last_used: dict[int, float] = {}

    async def predicate(interaction: Interaction) -> bool:
        if interaction.user.id in ADMIN_USERS:
            return True
        user_roles = [role.id for role in interaction.user.roles]
        if any(r in STAFF_ROLES for r in user_roles) or any(r in HELPER_ROLES for r in user_roles):
            return True

        now = time.monotonic()
        retry_after = seconds - (now - last_used.get(interaction.user.id, 0.0))
        if retry_after > 0:
            raise app_commands.CommandOnCooldown(app_commands.Cooldown(1, seconds), retry_after)

        last_used[interaction.user.id] = now
        return True

    return app_commands.check(predicate)