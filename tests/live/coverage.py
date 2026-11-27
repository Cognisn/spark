"""The live suite's coverage registry: every live case, what it proves and for which plan.

Each entry names one test in ``tests/live``, the suite (the workstream whose kick-off
it answers), the scope item of that kick-off, the rulings it proves and one line on
what it proves. ``docs/live-integration-coverage.md`` is this registry rendered;
``python -m tests.live.coverage`` writes it again after a change, and
``tests/live/test_coverage_registry.py`` (run by the default suite, no stack needed)
fails when a live test is missing here, an entry names no test, or the document is
out of date.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

DOCUMENT = "docs/live-integration-coverage.md"

#: The suites, by the workstream that wrote them, and what each kick-off covered.
SUITES: dict[str, str] = {
    "rc1": "test/0.3.0-live-e2e: the A18 rc1 checklist (items 0 to 11).",
    "conduit": "test/0.3.0-live-e2e-conduit: connections, credits and conduit (scopes 2 to 6).",
    "live-provider": "test/0.3.0-live-e2e: one live chat through a real service (item 6).",
    "projects-charts": (
        "test/0.3.0-projects-charts-verification: projects, charts, the panel and the "
        "built-in skills (scopes 1 to 5)."
    ),
}

RC1 = "tests/live/test_rc1.py::"
CONDUIT = "tests/live/test_conduit.py::"
PROJECTS = "tests/live/test_projects_live.py::"
CHARTS = "tests/live/test_charts_live.py::"
PERSONAL = "tests/live/test_personal_live.py::"
SKILLS = "tests/live/test_skills_live.py::"
SCREENS = "tests/live/test_screens_live.py::"


@dataclass(frozen=True)
class Case:
    id: str
    suite: str
    scope: str
    rulings: str
    test: str
    proves: str
    mode: str = "server"


CASES: tuple[Case, ...] = (
    # -- the rc1 checklist (A18) --------------------------------------------------------
    Case(
        "RC1-00",
        "rc1",
        "item 0",
        "A18",
        RC1 + "test_stack_has_the_created_administrator_and_two_invited_users",
        "The first administrator from bootstrap and two invited users exist.",
    ),
    Case(
        "RC1-11a",
        "rc1",
        "item 11",
        "A18",
        RC1 + "test_invitation_is_mailed_and_completed_from_the_link",
        "An invitation is mailed over STARTTLS and completed from its link.",
    ),
    Case(
        "RC1-11b",
        "rc1",
        "item 11",
        "A18",
        RC1 + "test_password_reset_is_mailed_and_completed_from_the_link",
        "A password reset is mailed, ends the old session and changes the password.",
    ),
    Case(
        "RC1-01",
        "rc1",
        "item 1",
        "A18",
        RC1 + "test_single_sign_on_against_the_oidc_provider",
        "Single sign-on against the OIDC provider behind the throwaway CA.",
    ),
    Case(
        "RC1-02",
        "rc1",
        "item 2",
        "A18",
        RC1 + "test_a_tool_category_a_user_may_not_have_is_refused",
        "A tool category a User may not have is neither offered nor run.",
    ),
    Case(
        "RC1-08",
        "rc1",
        "item 8",
        "A18",
        RC1 + "test_a_chat_attachment_is_never_seen_by_another_user",
        "One user's attachment is never listed, downloaded or read by another.",
    ),
    Case(
        "RC1-05",
        "rc1",
        "item 5",
        "A18",
        RC1 + "test_an_untrusted_tool_result_is_withheld_from_the_model",
        "An injected tool result is withheld from the model and recorded.",
    ),
    Case(
        "RC1-09",
        "rc1",
        "item 9",
        "A18",
        RC1 + "test_a_knowledge_graph_build_is_run_by_the_worker",
        "A knowledge-graph build is queued on web and run by the worker.",
    ),
    Case(
        "RC1-07",
        "rc1",
        "item 7",
        "A18",
        RC1 + "test_the_standby_worker_takes_over_when_the_active_one_stops",
        "The standby worker takes the lock when the active one stops.",
    ),
    Case(
        "RC1-10",
        "rc1",
        "item 10",
        "A18",
        RC1 + "test_a_connection_key_saved_on_one_replica_is_used_by_the_other",
        "A connection key saved on web is used by web2 without a restart.",
    ),
    Case(
        "RC1-04",
        "rc1",
        "item 4",
        "A18",
        RC1 + "test_a_personal_bundle_is_exported_and_imported_for_a_user",
        "A personal export imports for one user only; a second import is refused.",
    ),
    Case(
        "RC1-03",
        "rc1",
        "item 3",
        "A18, Q35",
        RC1 + "test_a_role_allowance_also_stops_the_plain_send_route",
        "A role allowance stops the plain send route without a model call.",
    ),
    # -- connections, credits and conduit -----------------------------------------------
    Case(
        "CON-2a",
        "conduit",
        "scope 2",
        "D101, D102",
        CONDUIT + "test_connections_of_two_services_discover_declare_and_refuse",
        "Connections of two services discover, take an overlay and refuse by residency.",
    ),
    Case(
        "CON-2b",
        "conduit",
        "scope 2",
        "Q54",
        CONDUIT + "test_a_model_declared_on_a_running_connection_can_be_used_at_once",
        "A model declared on a running connection is usable at once.",
    ),
    Case(
        "CON-3",
        "conduit",
        "scope 3",
        "D102",
        CONDUIT + "test_assignment_hides_a_connection_and_decides_each_allowance",
        "Assignment hides a connection and resolves each allowance.",
    ),
    Case(
        "CON-4",
        "conduit",
        "scope 4",
        "D102",
        CONDUIT + "test_credits_warn_at_80_stop_at_100_and_the_turn_in_flight_finishes",
        "Credits warn at 80%, stop at 100% and let the turn in flight finish.",
    ),
    Case(
        "CON-5a",
        "conduit",
        "scope 5",
        "A12",
        CONDUIT + "test_an_mcp_tool_over_http_through_conduit_is_called_and_audited",
        "An MCP tool over streamable HTTP is called through conduit and audited.",
    ),
    Case(
        "CON-5b",
        "conduit",
        "scope 5",
        "D102",
        CONDUIT + "test_a_skill_loaded_with_use_skill_reaches_the_model",
        "A skill an administrator imports is loaded with use_skill.",
    ),
    Case(
        "CON-5c",
        "conduit",
        "scope 5",
        "D102",
        CONDUIT + "test_a_forced_compaction_keeps_the_conversation_usable",
        "A forced compaction keeps the conversation usable.",
    ),
    Case(
        "CON-5d",
        "conduit",
        "scope 5",
        "Q55",
        CONDUIT + "test_the_stream_reports_a_compaction_after_the_reply",
        "The stream reports a compaction that ends a turn.",
    ),
    Case(
        "CON-6",
        "conduit",
        "scope 6",
        "D102",
        CONDUIT + "test_a_0_2_config_with_two_providers_becomes_two_connections_once",
        "A 0.2 config.yaml becomes two connections once, the key in the keychain.",
        "personal",
    ),
    Case(
        "LIVE-06",
        "live-provider",
        "item 6",
        "A18",
        "tests/live/test_live_provider.py::test_one_live_chat_through_the_named_connection",
        "One live chat through the connection named for the run (skipped without one).",
    ),
    # -- projects, charts, the panel and the built-in skills (wave 32) ------------------
    Case(
        "PCV-1.01",
        "projects-charts",
        "scope 1",
        "PC7, PC8",
        PROJECTS + "test_every_level_reaches_the_prompt_top_first_and_the_conversation_last",
        "Three levels of instructions reach the prompt top first, nearest wins, the conversation last.",
    ),
    Case(
        "PCV-1.02",
        "projects-charts",
        "scope 1",
        "PC10, PC11",
        PROJECTS + "test_a_contributors_resource_is_charged_to_the_owner_and_read_on_demand",
        "A Contributor's resource is charged to the owner, listed in the prompt and read on demand.",
    ),
    Case(
        "PCV-1.03",
        "projects-charts",
        "scope 1",
        "PC9, PC10",
        PROJECTS + "test_each_sharing_scope_decides_what_each_person_sees",
        "Owner, Contributor, Read-only and a non-member each see what their role and scope allow.",
    ),
    Case(
        "PCV-1.04",
        "projects-charts",
        "scope 1",
        "PC9",
        PROJECTS
        + "test_a_scope_change_hides_the_projects_conversations_but_not_an_individual_share",
        "Narrowing a scope hides the project's conversations but keeps an individual share.",
    ),
    Case(
        "PCV-1.05",
        "projects-charts",
        "scope 1",
        "PC9, PC16",
        PROJECTS + "test_a_conversation_in_a_project_is_shared_only_with_its_people",
        "A project conversation is shared only with the project's people (the picker limit).",
    ),
    Case(
        "PCV-1.05b",
        "projects-charts",
        "wave 33",
        "PC9",
        PROJECTS + "test_a_contributors_share_picker_offers_only_the_projects_people",
        "A Contributor's share picker offers only the project's people, by id, not a same-name outsider.",
    ),
    Case(
        "PCV-1.06",
        "projects-charts",
        "scope 1",
        "PC12",
        PROJECTS + "test_moves_warn_before_widening_or_narrowing_and_follow_the_rules",
        "Moves warn before widening or narrowing, go through no project, and only for the owner.",
    ),
    Case(
        "PCV-1.07",
        "projects-charts",
        "scope 1",
        "PC13",
        PROJECTS + "test_deleting_keeps_or_deletes_conversations_with_counts_and_notices",
        "Deleting keeps or deletes conversations, with counts, notices and mail.",
    ),
    Case(
        "PCV-1.08",
        "projects-charts",
        "scope 1",
        "PC14",
        PROJECTS + "test_transfer_moves_the_project_and_its_files_to_the_new_owner",
        "A transfer moves the tree, its files and their usage; the old owner stays a Contributor.",
    ),
    Case(
        "PCV-1.09",
        "projects-charts",
        "scope 1",
        "PC11, PC14, Q93",
        PROJECTS + "test_transfer_is_refused_when_the_new_owner_lacks_the_quota",
        "A transfer over the new owner's quota is refused.",
    ),
    Case(
        "PCV-1.10",
        "projects-charts",
        "scope 1",
        "PC2 to PC5",
        PROJECTS + "test_the_panel_lists_three_groups_and_renames_by_role",
        "The panel lists its three groups and renames by role, audited.",
    ),
    Case(
        "PCV-1.11",
        "projects-charts",
        "scope 1",
        "PC17",
        PROJECTS + "test_create_project_on_request_creates_it_and_refuses_without_one",
        "create_project creates on the user's request and refuses without one.",
    ),
    Case(
        "PCV-1.12",
        "projects-charts",
        "scope 1",
        "PC17",
        PROJECTS + "test_a_suggestion_is_a_card_dismissed_once_and_never_shown_again",
        "A suggestion is a card; after Dismiss no second suggestion is shown.",
    ),
    Case(
        "PCV-1.13",
        "projects-charts",
        "scope 1",
        "PC18",
        PROJECTS + "test_an_update_waits_for_the_owner_to_confirm_its_card",
        "An update changes nothing until its before-and-after card is accepted, once.",
    ),
    Case(
        "PCV-1.14",
        "projects-charts",
        "scope 1",
        "PC15",
        PERSONAL + "test_a_personal_bundle_v2_carries_its_projects_into_a_server_account",
        "A personal bundle version 2 imports its projects into a server account, unshared.",
        "both",
    ),
    Case(
        "PCV-2.01",
        "projects-charts",
        "scope 2",
        "PC5, PC19",
        CHARTS + "test_every_chart_type_is_drawn_from_inline_data_in_both_themes",
        "Every chart type is made from inline data and built for both themes.",
    ),
    Case(
        "PCV-2.02",
        "projects-charts",
        "scope 2",
        "PC19",
        CHARTS + "test_a_chart_reads_its_data_from_a_csv_and_from_an_excel_file",
        "A chart reads its data from a CSV file and from an Excel sheet.",
    ),
    Case(
        "PCV-2.03",
        "projects-charts",
        "scope 2",
        "PC20",
        CHARTS + "test_a_diagram_is_stored_with_its_title_and_served_as_its_source",
        "A Mermaid diagram is stored with its title and served as its source.",
    ),
    Case(
        "PCV-2.04",
        "projects-charts",
        "scope 2",
        "PC20, CS28",
        CHARTS + "test_a_member_of_the_shared_conversation_opens_a_chart_and_a_stranger_cannot",
        "A member of the shared conversation opens the charts and data; a stranger cannot.",
    ),
    Case(
        "PCV-2.05",
        "projects-charts",
        "scope 2",
        "PC19",
        CHARTS + "test_a_definition_the_validator_refuses_is_explained_to_the_model",
        "A refused definition is explained to the model and nothing is stored.",
    ),
    Case(
        "PCV-2.06",
        "projects-charts",
        "scope 2",
        "PC20",
        CHARTS + "test_charts_draw_in_the_message_and_the_pane_and_save_as_files",
        "In Chromium, cards draw in the message and pane in both themes; PNG, SVG and CSV save as files.",
    ),
    Case(
        "PCV-3.01",
        "projects-charts",
        "scope 3",
        "PC15",
        PERSONAL + "test_personal_projects_have_levels_resources_and_moves_without_sharing",
        "Personal projects have levels, resources and moves, and no sharing routes.",
        "personal",
    ),
    Case(
        "PCV-3.02",
        "projects-charts",
        "scope 3",
        "PC2 to PC5, PC19",
        PERSONAL + "test_personal_charts_and_the_panel",
        "Personal charts are listed in the panel with attachments and resources.",
        "personal",
    ),
    Case(
        "PCV-3.03",
        "projects-charts",
        "scope 3",
        "PC21",
        PERSONAL + "test_personal_mode_offers_the_built_in_skills",
        "Personal mode offers the three built-in skills and loads one.",
        "personal",
    ),
    Case(
        "PCV-4.01",
        "projects-charts",
        "scope 4",
        "PC21",
        SKILLS + "test_the_built_in_skills_are_offered_and_load",
        "The three built-in skills are offered to every user and load.",
    ),
    Case(
        "PCV-4.02",
        "projects-charts",
        "scope 4",
        "PC21",
        SKILLS + "test_an_administrator_switches_a_built_in_skill_off_and_on_for_everyone",
        "An administrator switches a built-in skill off for everyone and on again.",
    ),
    Case(
        "PCV-5.00",
        "projects-charts",
        "scope 5",
        "PC16, PC20",
        SCREENS + "test_the_served_chat_page_starts_without_a_script_error",
        "The served chat page starts without a script error and draws the chart card.",
    ),
    Case(
        "PCV-5.01",
        "projects-charts",
        "scope 5",
        "PC4, PC16",
        SCREENS + "test_the_screens_in_both_themes_at_desktop_and_phone_width",
        "The projects, chat and skills screens draw in both themes at desktop and phone width.",
    ),
)


def render() -> str:
    """The coverage document, from the registry."""
    lines = [
        "# Live Integration Coverage",
        "",
        "The live suite (`tests/live`) drives a running server-mode stack from `deploy/live`, "
        "and personal-mode SPARK on the same host, over HTTP and in a headless Chromium. Every "
        "test there is marked `integration` and skipped unless `SPARK_LIVE_STACK` names a "
        "stack, so CI and the wave gate deselect it; `deploy/live/RUNBOOK.md` says how to run "
        "it.",
        "",
        "This page is generated from the registry in `tests/live/coverage.py` with "
        "`python -m tests.live.coverage`. The default suite checks that every live test is "
        "registered and that this page matches the registry.",
        "",
        "## Suites",
        "",
    ]
    for name, what in SUITES.items():
        lines.append(f"- `{name}`: {what}")
    for name in SUITES:
        cases = [c for c in CASES if c.suite == name]
        lines += [
            "",
            f"## {name}",
            "",
            "| Case | Scope | Rulings | Mode | Proves | Test |",
            "|---|---|---|---|---|---|",
        ]
        for case in cases:
            test = case.test.split("::", 1)
            lines.append(
                f"| {case.id} | {case.scope} | {case.rulings} | {case.mode} | {case.proves} "
                f"| `{Path(test[0]).name}::{test[1]}` |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    target = Path(__file__).resolve().parents[2] / DOCUMENT
    target.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
