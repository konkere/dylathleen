from dylathleen.api.abstract.view import ViewComponent


class RecipeEditorComponent(ViewComponent):
    def __init__(self, text, warning_title, warning, readonly=False):
        super().__init__(None)
        self.value = text
        self.warning_title = warning_title
        self.warning = warning
        self.readonly = readonly
