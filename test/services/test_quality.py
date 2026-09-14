from app.services.quality import _material_entity_mismatches


def test_entity_check_rejects_generic_overlap_for_wrong_business_domain():
    report = {
        "shots": [
            {
                "shot": 1,
                "text": "血液透析水处理设备在医院机房运行。",
                "queries": ["血液透析 水处理 医院机房"],
                "accepted": {
                    "name": "净水机抠图透明图生成.png",
                    "category": "库尔斯特-商用净水直饮水",
                    "overlap_terms": ["处理"],
                },
            }
        ]
    }

    assert _material_entity_mismatches(report) == [1]


def test_entity_check_keeps_matching_dialysis_material():
    report = {
        "shots": [
            {
                "shot": 1,
                "text": "血液透析水处理机器人在洁净机房运行。",
                "queries": ["血液透析 水处理机器人 洁净机房"],
                "accepted": {
                    "name": "血透水处理机器人 机房现场.jpg",
                    "category": "科尔顿-医用-血液透析-AI机器人系列",
                    "overlap_terms": ["血液", "透析", "机器人"],
                },
            }
        ]
    }

    assert _material_entity_mismatches(report) == []
