from loguru import logger
from models.chat import GroupInfo, MessagesContainer
from models.requests import ListChannelsRequest
from models.responses import GroupInfoResponse, GroupsListResponse
from utils.decorators import make_async_background
from utils.storage import list_directories, load_json


@make_async_background
def list_channels(request: ListChannelsRequest) -> GroupsListResponse:
    """List groups/spaces in the Chat workspace with pagination."""
    try:
        group_dirs = list_directories("Groups")

        groups_list = []
        for group_dir in group_dirs:
            group_info_data = load_json(f"Groups/{group_dir}", "group_info.json")
            if group_info_data:
                try:
                    group_info = GroupInfo.model_validate(group_info_data)

                    messages_data = load_json(f"Groups/{group_dir}", "messages.json")
                    message_count = 0
                    if messages_data and "messages" in messages_data:
                        messages_container = MessagesContainer.model_validate(
                            messages_data
                        )
                        message_count = len(messages_container.messages)

                    groups_list.append(
                        {
                            "id": group_dir,
                            "name": group_info.name,
                            "member_count": len(group_info.members),
                            "message_count": message_count,
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse group {group_dir}: {e}")
                    continue

        start_idx = request.page * request.limit
        end_idx = start_idx + request.limit
        paginated_groups = groups_list[start_idx:end_idx]

        groups = [GroupInfoResponse.model_validate(group) for group in paginated_groups]

        return GroupsListResponse(
            groups=groups,
            total_count=len(groups_list),
            page=request.page,
            per_page=request.limit,
        )

    except Exception as e:
        logger.error(f"Error listing groups: {e}")
        raise ValueError(f"Error listing groups: {e}") from e
