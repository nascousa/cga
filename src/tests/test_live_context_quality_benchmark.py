from scripts.run_live_context_quality_benchmark import render_markdown, summarize_projects


def test_summarize_projects_averages_live_scores() -> None:
    report = {
        "cases": [
            {
                "project": "ProjectA",
                "baseline": {"hallucinationPressureScore": 20, "totalTokens": 1000},
                "cg": {"hallucinationPressureScore": 10, "totalTokens": 100},
                "comparison": {"hpsReductionPercent": 50, "tokenReductionPercent": 90},
            },
            {
                "project": "ProjectA",
                "baseline": {"hallucinationPressureScore": 10, "totalTokens": 500},
                "cg": {"hallucinationPressureScore": 5, "totalTokens": 50},
                "comparison": {"hpsReductionPercent": 50, "tokenReductionPercent": 90},
            },
            {
                "project": "ProjectB",
                "baseline": {"hallucinationPressureScore": 8, "totalTokens": 400},
                "cg": {"hallucinationPressureScore": 12, "totalTokens": 40},
                "comparison": {"hpsReductionPercent": -50, "tokenReductionPercent": 90},
            },
        ]
    }

    summary = summarize_projects(report)

    assert summary == [
        {
            "project": "ProjectA",
            "caseCount": 2,
            "avgBaselineHps": 15.0,
            "avgCgHps": 7.5,
            "avgHpsReductionPercent": 50.0,
            "avgBaselineTokens": 750.0,
            "avgCgTokens": 75.0,
            "avgTokenReductionPercent": 90.0,
        },
        {
            "project": "ProjectB",
            "caseCount": 1,
            "avgBaselineHps": 8.0,
            "avgCgHps": 12.0,
            "avgHpsReductionPercent": -50.0,
            "avgBaselineTokens": 400.0,
            "avgCgTokens": 40.0,
            "avgTokenReductionPercent": 90.0,
        },
    ]


def test_render_markdown_includes_live_repro_command() -> None:
    report = {
        "method": "hps-context-quality-v1",
        "liveBenchmark": {
            "runDate": "2026-06-02",
            "casesPerProject": 34,
            "projects": [
                {
                    "project": "ProjectA",
                    "projectId": "A1",
                    "graph": "projecta",
                    "validSymbolCandidates": 40,
                    "cases": 34,
                    "hostRepoPath": "D:/Repos/ProjectA",
                }
            ],
        },
        "summary": {
            "caseCount": 34,
            "avgBaselineHps": 20.0,
            "avgCgHps": 10.0,
            "avgHpsReductionPercent": 50.0,
            "avgBaselineTokens": 1000.0,
            "avgCgTokens": 100.0,
            "avgTokenReductionPercent": 90.0,
        },
        "projectSummary": [
            {
                "project": "ProjectA",
                "caseCount": 34,
                "avgBaselineHps": 20.0,
                "avgCgHps": 10.0,
                "avgHpsReductionPercent": 50.0,
                "avgBaselineTokens": 1000.0,
                "avgCgTokens": 100.0,
                "avgTokenReductionPercent": 90.0,
            }
        ],
    }

    markdown = render_markdown(report)

    assert "# ContextGraph Live Project HPS Benchmark" in markdown
    assert "Cases per project: 34" in markdown
    assert "--projects ProjectA" in markdown
    assert "The JSON report contains the full per-case scoring output and is intended as a local artifact." in markdown
    assert "Project ID" not in markdown
    assert "D:/Repos/ProjectA" not in markdown