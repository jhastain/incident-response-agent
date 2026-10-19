from google.adk.agents import LlmAgent

from ...config import create_fast_model
from ...shared_prompts import build_agent_instruction
from ...tools.bifrost_mcp import build_tool_list, truncate_tool_response
from ...tools.gcp_mcp import create_compute_engine_toolset
from ...tools.tool_resilience import keep_running_on_tool_error

EXTRA_CONTEXT = """
If the classification does NOT indicate Compute Engine / GCE infrastructure,
return immediately: "Not applicable — alert does not involve Compute Engine resources."

Only investigate when the alert references VMs, instance groups, managed
instance groups (MIGs), or GCE-hosted workloads.
"""

_compute_engine_mcp = create_compute_engine_toolset()

INSTRUCTION = build_agent_instruction(
    role="Compute Engine Infrastructure Investigator",
    context_refs=["{classification}", "{service_context}"],
    checklist=[
        "1. Check if the alert involves Compute Engine resources — if not, return 'Not applicable' immediately",
        "2. List instances in the affected GCP project — identify VMs related to the alert",
        "3. Get instance details for affected VMs — check status (RUNNING/TERMINATED/SUSPENDED/STAGING)",
        "4. Check attached disks for any affected instances — look for disk full or degraded status",
        "5. Check recent zone operations for errors — failed starts, failed disk attach, quota exceeded",
        "6. If instance groups are involved, check managed instance health and auto-healing status",
    ],
    tools_available=[
        "list_instances (Compute Engine MCP)",
        "get_instance_basic_info (Compute Engine MCP)",
        "list_instance_attached_disks (Compute Engine MCP)",
        "get_zone_operation (Compute Engine MCP)",
        "get_disk_basic_info (Compute Engine MCP)",
        "list_managed_instances (Compute Engine MCP)",
        "get_instance_group_manager_basic_info (Compute Engine MCP)",
        "list_instance_group_managers (Compute Engine MCP)",
    ],
    extra_context=EXTRA_CONTEXT,
)

compute_engine_health_agent = LlmAgent(
    model=create_fast_model(),
    name="compute_engine_health",
    description="Investigates Compute Engine VM instances, disks, and instance groups for infrastructure issues.",
    instruction=INSTRUCTION,
    output_key="findings_compute_engine",
    tools=build_tool_list(_compute_engine_mcp),
    after_tool_callback=truncate_tool_response,
    on_tool_error_callback=keep_running_on_tool_error,
)
