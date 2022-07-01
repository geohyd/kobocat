# KC



# class Middle:
    # def __init__(self, get_response):
        # self.get_response = get_response
		# print("Middleware KPI OK")
        # One-time configuration and initialization.

    # def __call__(self, request):
        # Code to be executed for each request before
        # the view (and later middleware) are called.
		# print("BEFORE VIEW")
        # response = self.get_response(request)
		# print("AFTER VIEW KPI")

        # Code to be executed for each request/response after
        # the view is called.

        # return response
class MultipleProxyMiddleware:
    FORWARDED_FOR_FIELDS = [
    'HTTP_X_FORWARDED_FOR',
    'HTTP_X_FORWARDED_HOST',
    'HTTP_X_FORWARDED_SERVER',
    'HTTP_HOST' 
    #<=== I ADDED THIS LINE
]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        """
        Rewrites the proxy headers so that only the most
        recent proxy is used.
        """
        for field in self.FORWARDED_FOR_FIELDS:
            if field in request.META:
                if ',' in request.META[field]:
                    parts = request.META[field].split(',')
                    request.META[field] = parts[-1].strip()
        return self.get_response(request)