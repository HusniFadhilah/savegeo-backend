from types import SimpleNamespace

from app.api.routes import workflows as workflow_routes


class WorkflowQuery:
    def __init__(self, rows):
        self.rows = rows
        self.batch_size = None

    def filter(self, *_conditions):
        return self

    def order_by(self, *_columns):
        return self

    def yield_per(self, batch_size):
        self.batch_size = batch_size
        return iter(self.rows)


def test_workflow_listing_pages_visible_rows_without_loading_all(monkeypatch):
    rows = [
        SimpleNamespace(owner_id=1, workflow_json={"visible": True}, to_dict=lambda _owner: {"id": "one"}),
        SimpleNamespace(owner_id=1, workflow_json={"visible": False}, to_dict=lambda _owner: {"id": "hidden"}),
        SimpleNamespace(owner_id=1, workflow_json={"visible": True}, to_dict=lambda _owner: {"id": "three"}),
        SimpleNamespace(owner_id=1, workflow_json={"visible": True}, to_dict=lambda _owner: {"id": "four"}),
    ]
    query = WorkflowQuery(rows)

    class DB:
        def query(self, model):
            assert model is workflow_routes.Workflow
            return query

    monkeypatch.setattr(workflow_routes, "viewer_feature_codes", lambda _db, _user: {"allowed"})
    monkeypatch.setattr(
        workflow_routes,
        "workflow_required_feature_codes",
        lambda definition: set() if definition["visible"] else {"restricted"},
    )
    result = workflow_routes.list_workflows(
        category=None,
        search=None,
        page=2,
        page_size=1,
        user=SimpleNamespace(id=1, username="owner"),
        db=DB(),
    )

    assert result == {"items": [{"id": "three"}], "page": 2, "pageSize": 1, "total": 3}
    assert query.batch_size == 250
