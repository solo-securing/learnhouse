from config.config import get_learnhouse_config
from src.services.utils.upload_content import upload_content
from src.services.utils.upload_gg_drive import upload_to_google_drive


async def upload_video(video_file, activity_uuid, org_uuid, course_uuid):
    video_format = video_file.filename.split(".")[-1]

    learnhouse_config = get_learnhouse_config()
    video_storage_type = learnhouse_config.video_storage_config.type

    if video_storage_type == "ggdrive":
        shareable_link = await upload_to_google_drive(
            course_uuid=course_uuid,
            activity_uuid=activity_uuid,
            type_of_dir='orgs',
            uuid=org_uuid,
            file_obj=video_file.file,
            file_and_format=f"video.{video_format}",
        )
        return {"message": "Video uploaded successfully", "shareable_link": shareable_link}
    else:
        await upload_content(
            directory=f"courses/{course_uuid}/activities/{activity_uuid}/video",
            type_of_dir='orgs',
            uuid=org_uuid,
            file_obj=video_file.file,
            file_and_format=f"video.{video_format}",
        )
        return {"message": "Video uploaded successfully"}
