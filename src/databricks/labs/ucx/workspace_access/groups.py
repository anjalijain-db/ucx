import functools
import json
import logging
import re
from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors.mapping import (
    BadRequest,
    DeadlineExceeded,
    InternalError,
    NotFound,
    ResourceConflict,
)
from databricks.sdk.retries import retried
from databricks.sdk.service import iam
from databricks.sdk.service.iam import Group

from databricks.labs.ucx.framework.crawlers import CrawlerBase, SqlBackend
from databricks.labs.ucx.framework.parallel import ManyError, Threads
from databricks.labs.ucx.framework.tui import Prompts
from databricks.labs.ucx.mixins.hardening import rate_limited

logger = logging.getLogger(__name__)


@dataclass
class MigratedGroup:
    id_in_workspace: str
    name_in_workspace: str
    name_in_account: str
    temporary_name: str
    members: str | None = None
    entitlements: str | None = None
    external_id: str | None = None
    roles: str | None = None

    @classmethod
    def partial_info(cls, workspace: iam.Group, account: iam.Group):
        """This method is only intended for use in tests"""
        assert workspace.id is not None
        assert workspace.display_name is not None
        assert account.display_name is not None
        return cls(
            id_in_workspace=workspace.id,
            name_in_workspace=workspace.display_name,
            name_in_account=account.display_name,
            temporary_name=f"tmp-{workspace.display_name}",
            external_id=workspace.external_id,
        )


class MigrationState:
    """Holds migration state of workspace-to-account groups"""

    def __init__(self, groups: list[MigratedGroup]):
        self._name_to_group: dict[str, MigratedGroup] = {_.name_in_workspace: _ for _ in groups}
        self._id_to_group: dict[str, MigratedGroup] = {_.id_in_workspace: _ for _ in groups}
        self.groups: list[MigratedGroup] = groups

    def get_target_principal(self, name: str) -> str | None:
        mg = self._name_to_group.get(name)
        if mg is None:
            return None
        return mg.name_in_account

    def is_in_scope(self, name: str) -> bool:
        if name is None:
            return False
        else:
            return name in self._name_to_group

    def __len__(self):
        return len(self._name_to_group)


class GroupMigrationStrategy:
    def __init__(
        self,
        workspace_groups_in_workspace,
        account_groups_in_account,
        /,
        renamed_groups_prefix,
        include_group_names=None,
    ):
        self.renamed_groups_prefix = renamed_groups_prefix
        self.workspace_groups_in_workspace = workspace_groups_in_workspace
        self.account_groups_in_account = account_groups_in_account
        self.include_group_names = include_group_names

    @abstractmethod
    def generate_migrated_groups(self):
        raise NotImplementedError

    def get_filtered_groups(self):
        if not self.include_group_names:
            logger.info("No group listing provided, all matching groups will be migrated")
            return self.workspace_groups_in_workspace
        logger.info("Group listing provided, a subset of all groups will be migrated")
        return {
            group_name: self.workspace_groups_in_workspace[group_name]
            for group_name in self.workspace_groups_in_workspace.keys()
            if group_name in self.include_group_names
        }

    @staticmethod
    def _safe_match(group_name: str, match_re: str) -> str:
        try:
            match = re.search(match_re, group_name)
            if not match:
                return group_name
            else:
                match_groups = match.groups()
            if match_groups:
                return match_groups[0]
            else:
                return match.group()
        except re.error:
            return group_name

    @staticmethod
    def _safe_sub(group_name: str, match_re: str, replace: str) -> str:
        try:
            return re.sub(match_re, replace, group_name)
        except re.error:
            logger.warning(f"Failed to apply Regex Expression {match_re} on Group Name {group_name}")
            return group_name


class MatchingNamesStrategy(GroupMigrationStrategy):
    def __init__(
        self,
        workspace_groups_in_workspace,
        account_groups_in_account,
        /,
        renamed_groups_prefix,
        include_group_names=None,
    ):
        super().__init__(
            workspace_groups_in_workspace,
            account_groups_in_account,
            include_group_names=include_group_names,
            renamed_groups_prefix=renamed_groups_prefix,
        )

    def generate_migrated_groups(self):
        workspace_groups = self.get_filtered_groups()
        for g in workspace_groups.values():
            temporary_name = f"{self.renamed_groups_prefix}{g.display_name}"
            account_group = self.account_groups_in_account.get(g.display_name)
            if not account_group:
                logger.info(f"Couldn't find a matching account group for {g.display_name} group")
                continue
            yield MigratedGroup(
                id_in_workspace=g.id,
                name_in_workspace=g.display_name,
                name_in_account=g.display_name,
                temporary_name=temporary_name,
                external_id=account_group.external_id,
                members=json.dumps([gg.as_dict() for gg in g.members]) if g.members else None,
                roles=json.dumps([gg.as_dict() for gg in g.roles]) if g.roles else None,
                entitlements=json.dumps([gg.as_dict() for gg in g.entitlements]) if g.entitlements else None,
            )


class MatchByExternalIdStrategy(GroupMigrationStrategy):
    def __init__(
        self,
        workspace_groups_in_workspace,
        account_groups_in_account,
        /,
        renamed_groups_prefix,
        include_group_names=None,
    ):
        super().__init__(
            workspace_groups_in_workspace,
            account_groups_in_account,
            include_group_names=include_group_names,
            renamed_groups_prefix=renamed_groups_prefix,
        )

    def generate_migrated_groups(self):
        workspace_groups = self.get_filtered_groups()
        account_groups_by_id = {group.external_id: group for group in self.account_groups_in_account.values()}
        for g in workspace_groups.values():
            temporary_name = f"{self.renamed_groups_prefix}{g.display_name}"
            account_group = account_groups_by_id.get(g.external_id)
            if account_group:
                yield MigratedGroup(
                    id_in_workspace=g.id,
                    name_in_workspace=g.display_name,
                    name_in_account=account_group.display_name,
                    temporary_name=temporary_name,
                    external_id=account_group.external_id,
                    members=json.dumps([gg.as_dict() for gg in g.members]) if g.members else None,
                    roles=json.dumps([gg.as_dict() for gg in g.roles]) if g.roles else None,
                    entitlements=json.dumps([gg.as_dict() for gg in g.entitlements]) if g.entitlements else None,
                )
            else:
                logger.info(f"Couldn't find a matching account group for {g.display_name} group with external_id")


class RegexSubStrategy(GroupMigrationStrategy):
    def __init__(
        self,
        workspace_groups_in_workspace,
        account_groups_in_account,
        /,
        renamed_groups_prefix,
        include_group_names=None,
        workspace_group_regex: str | None = None,
        workspace_group_replace: str | None = None,
    ):
        super().__init__(
            workspace_groups_in_workspace,
            account_groups_in_account,
            include_group_names=include_group_names,
            renamed_groups_prefix=renamed_groups_prefix,
        )
        self.workspace_group_replace = workspace_group_replace
        self.workspace_group_regex = workspace_group_regex

    def generate_migrated_groups(self):
        workspace_groups = self.get_filtered_groups()
        for g in workspace_groups.values():
            temporary_name = f"{self.renamed_groups_prefix}{g.display_name}"
            name_in_account = self._safe_sub(g.display_name, self.workspace_group_regex, self.workspace_group_replace)
            yield MigratedGroup(
                id_in_workspace=g.id,
                name_in_workspace=g.display_name,
                name_in_account=name_in_account,
                temporary_name=temporary_name,
                external_id=self.account_groups_in_account[name_in_account].external_id,
                members=json.dumps([gg.as_dict() for gg in g.members]) if g.members else None,
                roles=json.dumps([gg.as_dict() for gg in g.roles]) if g.roles else None,
                entitlements=json.dumps([gg.as_dict() for gg in g.entitlements]) if g.entitlements else None,
            )


class RegexMatchStrategy(GroupMigrationStrategy):
    def __init__(
        self,
        workspace_groups_in_workspace,
        account_groups_in_account,
        /,
        renamed_groups_prefix,
        include_group_names=None,
        workspace_group_regex: str | None = None,
        account_group_regex: str | None = None,
    ):
        super().__init__(
            workspace_groups_in_workspace,
            account_groups_in_account,
            include_group_names=include_group_names,
            renamed_groups_prefix=renamed_groups_prefix,
        )
        self.account_group_regex = account_group_regex
        self.workspace_group_regex = workspace_group_regex

    def generate_migrated_groups(self):
        workspace_groups_by_match = {
            self._safe_match(group_name, self.workspace_group_regex): group
            for group_name, group in self.get_filtered_groups().items()
        }
        account_groups_by_match = {
            self._safe_match(group_name, self.account_group_regex): group
            for group_name, group in self.account_groups_in_account.items()
        }
        for group_match, ws_group in workspace_groups_by_match.items():
            temporary_name = f"{self.renamed_groups_prefix}{ws_group.display_name}"
            account_group = account_groups_by_match.get(group_match)
            if account_group:
                yield MigratedGroup(
                    id_in_workspace=ws_group.id,
                    name_in_workspace=ws_group.display_name,
                    name_in_account=account_group.display_name,
                    temporary_name=temporary_name,
                    external_id=account_group.external_id,
                    members=json.dumps([gg.as_dict() for gg in ws_group.members]) if ws_group.members else None,
                    roles=json.dumps([gg.as_dict() for gg in ws_group.roles]) if ws_group.roles else None,
                    entitlements=json.dumps([gg.as_dict() for gg in ws_group.entitlements])
                    if ws_group.entitlements
                    else None,
                )
            else:
                logger.info(f"Couldn't find a match for group {ws_group.display_name}")


class GroupManager(CrawlerBase[MigratedGroup]):
    _SYSTEM_GROUPS: ClassVar[list[str]] = ["users", "admins", "account users"]

    def __init__(
        self,
        sql_backend: SqlBackend,
        ws: WorkspaceClient,
        inventory_database: str,
        include_group_names: list[str] | None = None,
        renamed_group_prefix: str | None = "ucx-renamed-",
        workspace_group_regex: str | None = None,
        workspace_group_replace: str | None = None,
        account_group_regex: str | None = None,
        verify_timeout: timedelta | None = timedelta(minutes=2),
        *,
        external_id_match: bool = False,
    ):
        super().__init__(sql_backend, "hive_metastore", inventory_database, "groups", MigratedGroup)
        if not renamed_group_prefix:
            renamed_group_prefix = "ucx-renamed-"

        self._ws = ws
        self._include_group_names = include_group_names
        self._renamed_group_prefix = renamed_group_prefix
        self._workspace_group_regex = workspace_group_regex
        self._workspace_group_replace = workspace_group_replace
        self._account_group_regex = account_group_regex
        self._external_id_match = external_id_match
        self._verify_timeout = verify_timeout

    def snapshot(self) -> list[MigratedGroup]:
        return self._snapshot(self._fetcher, self._crawler)

    def has_groups(self) -> bool:
        return len(self.snapshot()) > 0

    def rename_groups(self):
        tasks = []
        account_groups_in_workspace = self._account_groups_in_workspace()
        workspace_groups_in_workspace = self._workspace_groups_in_workspace()
        groups_to_migrate = self.get_migration_state().groups

        for mg in groups_to_migrate:
            if mg.name_in_account in account_groups_in_workspace:
                logger.info(f"Skipping {mg.name_in_account}: already in workspace")
                continue
            if mg.temporary_name in workspace_groups_in_workspace:
                logger.info(f"Skipping {mg.name_in_workspace}: already renamed")
                continue
            logger.info(f"Renaming: {mg.name_in_workspace} -> {mg.temporary_name}")
            tasks.append(functools.partial(self._rename_group, mg.id_in_workspace, mg.temporary_name))
        _, errors = Threads.gather("rename groups in the workspace", tasks)
        if len(errors) > 0:
            raise ManyError(errors)

    def _rename_group(self, group_id: str, new_group_name: str):
        ops = [iam.Patch(iam.PatchOp.REPLACE, "displayName", new_group_name)]
        self._ws.groups.patch(group_id, operations=ops)
        return True

    def reflect_account_groups_on_workspace(self):
        tasks = []
        account_groups_in_account = self._account_groups_in_account()
        account_groups_in_workspace = self._account_groups_in_workspace()
        groups_to_migrate = self.get_migration_state().groups
        for mg in groups_to_migrate:
            if mg.name_in_account in account_groups_in_workspace:
                logger.info(f"Skipping {mg.name_in_account}: already in workspace")
                continue
            if mg.name_in_account not in account_groups_in_account:
                logger.warning(f"Skipping {mg.name_in_account}: not in account")
                continue
            group_id = account_groups_in_account[mg.name_in_account].id
            tasks.append(functools.partial(self._reflect_account_group_to_workspace, group_id))
        _, errors = Threads.gather("reflect account groups on this workspace", tasks)
        if len(errors) > 0:
            raise ManyError(errors)

    def get_migration_state(self) -> MigrationState:
        return MigrationState(self.snapshot())

    def delete_original_workspace_groups(self):
        tasks = []
        workspace_groups_in_workspace = self._workspace_groups_in_workspace()
        account_groups_in_workspace = self._account_groups_in_workspace()
        for mg in self.snapshot():
            if mg.temporary_name not in workspace_groups_in_workspace:
                logger.info(f"Skipping {mg.name_in_workspace}: no longer in workspace")
                continue
            if mg.name_in_account not in account_groups_in_workspace:
                logger.info(f"Skipping {mg.name_in_account}: not reflected in workspace")
                continue
            tasks.append(functools.partial(self._delete_workspace_group, mg.id_in_workspace, mg.temporary_name))
        _, errors = Threads.gather("removing original workspace groups", tasks)
        if len(errors) > 0:
            logger.error(f"During account-to-workspace reflection got {len(errors)} errors. See debug logs")
            raise ManyError(errors)

    def _fetcher(self) -> Iterable[MigratedGroup]:
        for row in self._backend.fetch(f"SELECT * FROM {self._full_name}"):
            yield MigratedGroup(*row)

    def _crawler(self) -> Iterable[MigratedGroup]:
        workspace_groups_in_workspace = self._workspace_groups_in_workspace()
        account_groups_in_account = self._account_groups_in_account()
        strategy = self._get_strategy(workspace_groups_in_workspace, account_groups_in_account)
        yield from strategy.generate_migrated_groups()

    def _workspace_groups_in_workspace(self) -> dict[str, Group]:
        attributes = "id,displayName,meta,externalId,members,roles,entitlements"
        groups = {}
        for g in self._list_workspace_groups("WorkspaceGroup", attributes):
            if not g.display_name:
                continue
            groups[g.display_name] = g
        return groups

    def _account_groups_in_workspace(self) -> dict[str, Group]:
        groups = {}
        for g in self._list_workspace_groups("Group", "id,displayName,externalId,meta"):
            if not g.display_name:
                continue
            groups[g.display_name] = g
        return groups

    def _account_groups_in_account(self) -> dict[str, Group]:
        groups = {}
        for g in self._list_account_groups("id,displayName,externalId"):
            if not g.display_name:
                continue
            groups[g.display_name] = g
        return groups

    def _is_group_out_of_scope(self, group: iam.Group, resource_type: str) -> bool:
        if group.display_name in self._SYSTEM_GROUPS:
            return True
        meta = group.meta
        if not meta:
            return False
        if meta.resource_type != resource_type:
            return True
        return False

    def _list_workspace_groups(self, resource_type: str, scim_attributes: str) -> list[iam.Group]:
        results = []
        logger.info(f"Listing workspace groups (resource_type={resource_type}) with {scim_attributes}...")
        # these attributes can get too large causing the api to timeout
        # so we're fetching groups without these attributes first
        # and then calling get on each of them to fetch all attributes
        attributes = scim_attributes.split(",")
        if "members" in attributes:
            attributes.remove("members")
            retry_on_internal_error = retried(on=[InternalError], timeout=self._verify_timeout)
            get_group = retry_on_internal_error(self._get_group)
            for g in self._ws.groups.list(attributes=",".join(attributes)):
                if self._is_group_out_of_scope(g, resource_type):
                    continue
                group_with_all_attributes = get_group(g.id)
                if not group_with_all_attributes:
                    continue
                results.append(group_with_all_attributes)
        else:
            for g in self._ws.groups.list(attributes=scim_attributes):
                if self._is_group_out_of_scope(g, resource_type):
                    continue
                results.append(g)
        logger.info(f"Found {len(results)} {resource_type}")
        return results

    @rate_limited(max_requests=255, burst_period_seconds=60)
    def _get_group(self, group_id: str) -> iam.Group | None:
        try:
            return self._ws.groups.get(group_id)
        except NotFound:
            # during integration tests, we may get certain groups removed,
            # which will cause timeout errors because of groups no longer there.
            return None

    def _list_account_groups(self, scim_attributes: str) -> list[iam.Group]:
        # TODO: we should avoid using this method, as it's not documented
        # get account-level groups even if they're not (yet) assigned to a workspace
        logger.info(f"Listing account groups with {scim_attributes}...")
        account_groups = []
        raw = self._ws.api_client.do("GET", "/api/2.0/account/scim/v2/Groups", query={"attributes": scim_attributes})
        for r in raw.get("Resources", []):  # type: ignore[union-attr]
            g = iam.Group.from_dict(r)
            if g.display_name in self._SYSTEM_GROUPS:
                continue
            account_groups.append(g)
        logger.info(f"Found {len(account_groups)} account groups")
        sorted_groups: list[iam.Group] = sorted(account_groups, key=lambda _: _.display_name)  # type: ignore[arg-type,return-value]
        return sorted_groups

    @retried(on=[InternalError, ResourceConflict, DeadlineExceeded])
    @rate_limited(max_requests=35, burst_period_seconds=60)
    def _delete_workspace_group(self, group_id: str, display_name: str) -> None:
        try:
            logger.info(f"Deleting the workspace-level group {display_name} with id {group_id}")
            self._ws.groups.delete(id=group_id)
            logger.info(f"Workspace-level group {display_name} with id {group_id} was deleted")
            return None
        except NotFound:
            return None

    @retried(on=[InternalError, ResourceConflict, DeadlineExceeded])
    @rate_limited(max_requests=10)
    def _reflect_account_group_to_workspace(self, account_group_id: str):
        try:
            # TODO: add OpenAPI spec for it
            path = f"/api/2.0/preview/permissionassignments/principals/{account_group_id}"
            self._ws.api_client.do("PUT", path, data=json.dumps({"permissions": ["USER"]}))
            return True
        except BadRequest:
            # already exists
            return True

    def _get_strategy(
        self, workspace_groups_in_workspace: dict[str, Group], account_groups_in_account: dict[str, Group]
    ) -> GroupMigrationStrategy:
        if self._workspace_group_regex and self._workspace_group_replace:
            return RegexSubStrategy(
                workspace_groups_in_workspace,
                account_groups_in_account,
                renamed_groups_prefix=self._renamed_group_prefix,
                include_group_names=self._include_group_names,
                workspace_group_regex=self._workspace_group_regex,
                workspace_group_replace=self._workspace_group_replace,
            )
        if self._workspace_group_regex and self._account_group_regex:
            return RegexMatchStrategy(
                workspace_groups_in_workspace,
                account_groups_in_account,
                renamed_groups_prefix=self._renamed_group_prefix,
                include_group_names=self._include_group_names,
                workspace_group_regex=self._workspace_group_regex,
                account_group_regex=self._account_group_regex,
            )
        if self._external_id_match:
            return MatchByExternalIdStrategy(
                workspace_groups_in_workspace,
                account_groups_in_account,
                renamed_groups_prefix=self._renamed_group_prefix,
                include_group_names=self._include_group_names,
            )
        return MatchingNamesStrategy(
            workspace_groups_in_workspace,
            account_groups_in_account,
            renamed_groups_prefix=self._renamed_group_prefix,
            include_group_names=self._include_group_names,
        )


class ConfigureGroups:
    renamed_group_prefix = "db-temp-"
    workspace_group_regex = None
    workspace_group_replace = None
    account_group_regex = None
    group_match_by_external_id = None
    include_group_names = None

    def __init__(self, prompts: Prompts):
        self._prompts = prompts
        self._ask_for_group = functools.partial(self._prompts.question, validate=self._is_valid_group_str)
        self._ask_for_regex = functools.partial(self._prompts.question, validate=self._validate_regex)

    def run(self):
        self.renamed_group_prefix = self._ask_for_group("Backup prefix", default=self.renamed_group_prefix)
        strategy = self._prompts.choice_from_dict(
            "Choose how to map the workspace groups:",
            {
                "Apply a Prefix": self._configure_prefix,
                "Apply a Suffix": self._configure_suffix,
                "Comma-separated list of workspace group names to migrate": self._configure_names,
                "Match By External ID": self._configure_external,
                "Regex Substitution": self._configure_substitution,
                "Regex Matching": self._configure_matching,
            },
        )
        strategy()

    def _configure_prefix(self):
        prefix = self._ask_for_group("Enter a prefix to add to the workspace group name")
        if not prefix:
            return False
        self.workspace_group_regex = "^"
        self.workspace_group_replace = prefix
        return True

    def _configure_suffix(self):
        suffix = self._ask_for_group("Enter a suffix to add to the workspace group name")
        if not suffix:
            return False
        self.workspace_group_regex = "$"
        self.workspace_group_replace = suffix
        return True

    def _configure_substitution(self):
        match_value = self._ask_for_regex("Enter a regular expression for substitution")
        if not match_value:
            return False
        sub_value = self._ask_for_group("Enter the substitution value")
        if not sub_value:
            return False
        self.workspace_group_regex = match_value
        self.workspace_group_replace = sub_value
        return True

    def _configure_matching(self):
        ws_match_value = self._ask_for_regex("Enter a regular expression to match on the workspace group")
        if not ws_match_value:
            return False
        acct_match_value = self._ask_for_regex("Enter a regular expression to match on the account group")
        if not acct_match_value:
            return False
        self.workspace_group_regex = ws_match_value
        self.account_group_regex = acct_match_value
        return True

    def _configure_names(self):
        selected_groups = self._prompts.question(
            "Comma-separated list of workspace group names to migrate. If not specified, we'll use all "
            "account-level groups with matching names to workspace-level groups",
            default="<ALL>",
        )
        if selected_groups != "<ALL>":
            self.include_group_names = [x.strip() for x in selected_groups.split(",")]
        return True

    def _configure_external(self):
        self.group_match_by_external_id = True
        return True

    @staticmethod
    def _is_valid_group_str(group_str: str):
        return group_str and not re.search(r"[\s#,+ \\<>;]", group_str)

    @staticmethod
    def _validate_regex(regex_input: str) -> bool:
        try:
            re.compile(regex_input)
            return True
        except re.error:
            logger.error(f"{regex_input} is an invalid regular expression")
            return False
