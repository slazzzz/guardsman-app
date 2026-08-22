### PERMISSION CHECK DECORATORS ###

from discord import Interaction, app_commands

from bot.config import ADMIN_USERS, MEMBER_ROLES, STAFF_ROLES


def is_staff():
    async def predicate(interaction: Interaction):
        user_roles = [role.id for role in interaction.user.roles]
        return any(r in STAFF_ROLES for r in user_roles)
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


def is_allowed():
    async def predicate(interaction: Interaction):
        # Check user ID
        if interaction.user.id in ADMIN_USERS:
            return True

        # Check roles
        if any(role.id in STAFF_ROLES for role in interaction.user.roles):
            return True

        # Check permissions
        if interaction.user.guild_permissions.manage_guild:
            return True

        return False

    return app_commands.check(predicate)
