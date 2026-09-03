from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "qianduan" / "html-main2"


def test_new_role_page_never_hides_data_for_algorithm_label() -> None:
    page = (ROOT / "emerging-roles.html").read_text(encoding="utf-8")

    assert "expectedAlgorithmVersion" not in page
    assert "旧版候选已停止展示" not in page
    assert "正式版正在重新计算" not in page
    assert '.filter((item) => item.algorithm_version' not in page
    assert 'requestEvolution("/runs/latest-public")' in page


def test_panorama_has_no_below_graph_inspector_module() -> None:
    page = (ROOT / "panorama.html").read_text(encoding="utf-8")

    assert 'class="panel graph-inspector"' not in page
    assert 'id="detailTitle"' not in page
    assert "当前能力在岗位数据中出现了" not in page
    assert "RawJDVersion" not in page
    assert "点击能力点看招聘依据" not in page


def test_maintenance_page_has_a_human_label_for_cancelled_runs() -> None:
    page = (ROOT / "maintenance.html").read_text(encoding="utf-8")

    assert 'cancelled:"本轮已停止，等待下次运行"' in page
    assert 'cancelled:"本轮采集已停止，当前图谱继续正常使用"' in page
    assert 'cancelled:"本轮未发布，保留当前图谱"' in page


def test_home_page_exposes_only_the_continuous_radar_status() -> None:
    page = (ROOT / "index.html").read_text(encoding="utf-8")

    assert '<section class="card radar-status-card"' in page
    assert page.count("岗位雷达持续检测中") == 1
    assert 'id="radarStatusTitle">岗位雷达持续检测中</h2>' in page
    assert "快速测试" not in page
    assert "查看结果" not in page
    assert "radarMode" not in page
    assert "startRadar" not in page
    assert "radarReviewLink" not in page
    assert "/api/v1/radar/config" not in page
    assert "/api/v1/radar/status" not in page
    assert "/api/v1/radar/runs" not in page


def test_home_page_keeps_formal_module_entry_points() -> None:
    page = (ROOT / "index.html").read_text(encoding="utf-8")

    assert 'href="panorama.html"' in page
    assert 'href="emerging-roles.html"' in page
    assert 'href="emerging-roles.html?view=updates"' in page
    assert 'href="resume-match.html"' in page
    assert "旧版" not in page
    assert "新版" not in page


def test_home_radar_status_has_responsive_styles() -> None:
    styles = (ROOT / "assets" / "ui.css").read_text(encoding="utf-8")

    assert ".radar-status-card" in styles
    assert ".radar-status-content h2" in styles
    assert ".radar-controls" not in styles
    assert ".radar-progress" not in styles


def test_panorama_exposes_51job_closed_loop_contract() -> None:
    page = (ROOT / "panorama.html").read_text(encoding="utf-8")
    styles = (ROOT / "assets" / "ui.css").read_text(encoding="utf-8")

    assert "/api/v1/roles/${encodeURIComponent(roleId)}/openings?page=${safePage}&page_size=${OPENINGS_PAGE_SIZE}" in page
    assert 'id="roleOpenings"' in page
    assert 'id="openingsCta"' in page
    assert "查看企业招聘机会" in page
    assert "function renderOpeningsPagination" in page
    assert "data-openings-page" in page
    assert "function safe51jobUrl" in page
    assert 'host !== "51job.com"' in page
    assert 'target="_blank" rel="noopener noreferrer"' in page
    assert "查看岗位详细信息" in page
    assert "招聘信息具有时效性" in page
    assert "该岗位当前没有可直接跳转" in page
    assert ".opening-card" in styles
    assert ".openings-grid" in styles
    assert ".openings-pagination" in styles
    assert ".graph-cta-row" in styles
