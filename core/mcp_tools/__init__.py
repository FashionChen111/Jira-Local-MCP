"""显式公开 Jira 查询、入队和本地队列生命周期 Tool，供服务统一注册。"""

from core.mcp_tools.jira_query import (
    jira_query_issue,
    jira_query_comments,
    jira_query_worklogs,
    jira_query_attachments,
    jira_jql_search,
)
from core.mcp_tools.jira_enqueue import (
    jira_issue_create_append,
    jira_issue_clone_append,
    jira_issue_move_append,
    jira_issue_type_change_append,
    jira_issue_transition_append,
    jira_field_update_append,
    jira_customfield_update_append,
    jira_attachment_add_append,
    jira_comment_add_append,
    jira_worklog_add_append,
    jira_issuelink_add_append,
)
from core.mcp_tools.queue_tool import (
    jira_list_queue,
    jira_get_queue_operations,
    jira_list_execution_state,
    jira_cancel_queue_operations,
    jira_get_execution_log,
)


TOOLS = (
    jira_list_queue,
    jira_get_queue_operations,
    jira_list_execution_state,
    jira_cancel_queue_operations,
    jira_query_issue,
    jira_query_comments,
    jira_query_worklogs,
    jira_query_attachments,
    jira_jql_search,
    jira_issue_create_append,
    jira_issue_clone_append,
    jira_issue_move_append,
    jira_issue_type_change_append,
    jira_issue_transition_append,
    jira_field_update_append,
    jira_customfield_update_append,
    jira_attachment_add_append,
    jira_comment_add_append,
    jira_worklog_add_append,
    jira_issuelink_add_append,
    jira_get_execution_log,
)

__all__ = [
    "TOOLS",
    "jira_list_queue",
    "jira_get_queue_operations",
    "jira_list_execution_state",
    "jira_cancel_queue_operations",
    "jira_query_issue",
    "jira_query_comments",
    "jira_query_worklogs",
    "jira_query_attachments",
    "jira_jql_search",
    "jira_issue_create_append",
    "jira_issue_clone_append",
    "jira_issue_move_append",
    "jira_issue_type_change_append",
    "jira_issue_transition_append",
    "jira_field_update_append",
    "jira_customfield_update_append",
    "jira_attachment_add_append",
    "jira_comment_add_append",
    "jira_worklog_add_append",
    "jira_issuelink_add_append",
    "jira_get_execution_log",
]
