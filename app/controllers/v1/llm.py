from fastapi import Depends, Request
from loguru import logger

from app.auth.deps import _get_current_user
from app.controllers.v1.base import new_router
from app.models.schema import (
    VideoScriptRequest,
    VideoScriptResponse,
    VideoSocialMetadataRequest,
    VideoSocialMetadataResponse,
    VideoTermsRequest,
    VideoTermsResponse,
)
from app.services import llm
from app.services.kb_client import kb_client
from app.utils import utils

# authentication dependency
# router = new_router(dependencies=[Depends(base.verify_token)])
router = new_router(dependencies=[Depends(_get_current_user)])


@router.post(
    "/scripts",
    response_model=VideoScriptResponse,
    summary="Create a script for the video",
)
def generate_video_script(request: Request, body: VideoScriptRequest):
    use_kb = getattr(body, "use_knowledge", False)
    kb_docs = getattr(body, "kb_doc_filenames", None)

    # 素材匹配脚本：素材优先，基于知识库素材生成脚本（主题作为叙事主线）
    match_materials = getattr(body, "match_materials_to_script", False)
    video_source = getattr(body, "video_source", None) or "pexels"
    kb_category = getattr(body, "kb_category", None) or ""

    if match_materials and video_source in ("knowledge_base", "jimeng"):
        kb_media = []
        try:
            if kb_category:
                kb_media = kb_client.list_media_sampled(kb_category)
            else:
                kb_media = kb_client.relevant_media(
                    body.video_subject or "", top_k=40, category=""
                )
        except Exception as _e:
            logger.warning(f"generate_script: material fetch failed: {_e}")

        if kb_media:
            storyboard = llm.generate_script_from_materials(
                kb_media=kb_media,
                language=body.video_language,
                paragraph_number=body.paragraph_number,
                video_subject=body.video_subject,
                video_script_prompt=body.video_script_prompt,
                custom_system_prompt=body.custom_system_prompt,
                target_duration=getattr(body, "video_script_duration", 0) or 0,
                knowledge_context="",
            )
            if storyboard:
                _parts = [
                    str(s.get("text", "")).strip()
                    for s in storyboard
                    if str(s.get("text", "")).strip()
                ]
                _script = "\n\n".join(_parts).strip()
                if _script:
                    return utils.get_response(
                        200,
                        {
                            "video_script": _script,
                            "kb_info": {
                                "used": True,
                                "fallback": False,
                                "chunks": len(kb_media),
                                "empty": False,
                            },
                        },
                    )

    result = llm.generate_script(
        video_subject=body.video_subject,
        language=body.video_language,
        paragraph_number=body.paragraph_number,
        video_script_prompt=body.video_script_prompt,
        custom_system_prompt=body.custom_system_prompt,
        use_knowledge=use_kb,
        kb_doc_filenames=kb_docs,
        target_duration=getattr(body, "video_script_duration", 0) or 0,
    )
    # Handle tuple return (script, kb_info) or plain string
    if isinstance(result, tuple):
        video_script, kb_info = result
    else:
        video_script = result
        kb_info = {}
    response = {"video_script": video_script, "kb_info": kb_info}
    return utils.get_response(200, response)


@router.post(
    "/terms",
    response_model=VideoTermsResponse,
    summary="Generate video terms based on the video script",
)
def generate_video_terms(request: Request, body: VideoTermsRequest):
    video_source = getattr(body, "video_source", None) or "pexels"
    video_terms = llm.generate_terms(
        video_subject=body.video_subject,
        video_script=body.video_script,
        amount=body.amount,
        match_script_order=body.match_materials_to_script,
        source=video_source,
    )
    response = {"video_terms": video_terms}
    return utils.get_response(200, response)


@router.post(
    "/social-metadata",
    response_model=VideoSocialMetadataResponse,
    summary="Generate social publishing metadata",
)
def generate_video_social_metadata(
    request: Request, body: VideoSocialMetadataRequest
):
    metadata = llm.generate_social_metadata(
        video_subject=body.video_subject,
        video_script=body.video_script,
        language=body.language,
        platform=body.platform,
    )
    return utils.get_response(200, metadata)
