# KC



class Middle:
    def __init__(self, get_response):
        self.get_response = get_response
		print("Middleware KPI OK")
        # One-time configuration and initialization.

    def __call__(self, request):
        # Code to be executed for each request before
        # the view (and later middleware) are called.
		print("BEFORE VIEW")
        response = self.get_response(request)
		print("AFTER VIEW KPI")

        # Code to be executed for each request/response after
        # the view is called.

        return response
