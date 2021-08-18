# coding: utf-8
from __future__ import unicode_literals, print_function, division, absolute_import

from os import path

from onadata.apps.api.tests.viewsets.test_abstract_viewset import \
    TestAbstractViewSet
from onadata.apps.api.viewsets.attachment_viewset import AttachmentViewSet


class TestAttachmentViewSet(TestAbstractViewSet):

    def setUp(self):
        super(TestAttachmentViewSet, self).setUp()
        self.retrieve_view = AttachmentViewSet.as_view({
            'get': 'retrieve'
        })
        self.list_view = AttachmentViewSet.as_view({
            'get': 'list'
        })

        self.publish_xls_form()

    def test_retrieve_view(self):
        self._submit_transport_instance_w_attachment()

        pk = self.attachment.pk
        data = {
            'url': 'http://testserver/api/v1/media/%s' % pk,
            'field_xpath': None,
            'download_url': self.attachment.secure_url(),
            'small_download_url': self.attachment.secure_url('small'),
            'medium_download_url': self.attachment.secure_url('medium'),
            'large_download_url': self.attachment.secure_url('large'),
            'id': pk,
            'xform': self.xform.pk,
            'instance': self.attachment.instance.pk,
            'mimetype': self.attachment.mimetype,
            'filename': self.attachment.media_file.name
        }
        request = self.factory.get('/', **self.extra)
        response = self.retrieve_view(request, pk=pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(isinstance(response.data, dict))

        self.assertEqual(response.data, data)

        # file download
        filename = data['filename']
        ext = filename[filename.rindex('.') + 1:]
        request = self.factory.get('/', **self.extra)
        response = self.retrieve_view(request, pk=pk, format=ext)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'image/jpeg')

    def test_list_view(self):
        self._submit_transport_instance_w_attachment()

        request = self.factory.get('/', **self.extra)
        response = self.list_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(isinstance(response.data, list))

    def test_list_view_filter_by_xform(self):
        self._submit_transport_instance_w_attachment()

        data = {
            'xform': self.xform.pk
        }
        request = self.factory.get('/', data, **self.extra)
        response = self.list_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(isinstance(response.data, list))

        data['xform'] = 10000000
        request = self.factory.get('/', data, **self.extra)
        response = self.list_view(request)
        self.assertEqual(response.status_code, 404)

        data['xform'] = 'lol'
        request = self.factory.get('/', data, **self.extra)
        response = self.list_view(request)
        self.assertEqual(response.status_code, 400)

    def test_list_view_filter_by_instance(self):
        self._submit_transport_instance_w_attachment()

        data = {
            'instance': self.attachment.instance.pk
        }
        request = self.factory.get('/', data, **self.extra)
        response = self.list_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(isinstance(response.data, list))

        data['instance'] = 10000000
        request = self.factory.get('/', data, **self.extra)
        response = self.list_view(request)
        self.assertEqual(response.status_code, 404)

        data['instance'] = 'lol'
        request = self.factory.get('/', data, **self.extra)
        response = self.list_view(request)
        self.assertEqual(response.status_code, 400)

    def test_direct_image_link(self):
        self._submit_transport_instance_w_attachment()

        data = {
            'filename': self.attachment.media_file.name
        }
        request = self.factory.get('/', data, **self.extra)
        response = self.retrieve_view(request, pk=self.attachment.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(isinstance(response.data, basestring))
        self.assertEqual(response.data, self.attachment.secure_url())

        data['filename'] = 10000000
        request = self.factory.get('/', data, **self.extra)
        response = self.retrieve_view(request, pk=self.attachment.instance.pk)
        self.assertEqual(response.status_code, 404)

        data['filename'] = 'lol'
        request = self.factory.get('/', data, **self.extra)
        response = self.retrieve_view(request, pk=self.attachment.instance.pk)
        self.assertEqual(response.status_code, 404)

    def test_direct_image_link_uppercase(self):
        self._submit_transport_instance_w_attachment(
            media_file="1335783522564.JPG")

        filename = self.attachment.media_file.name
        file_base, file_extension = path.splitext(filename)
        data = {
            'filename': file_base + file_extension.upper()
        }
        request = self.factory.get('/', data, **self.extra)
        response = self.retrieve_view(request, pk=self.attachment.pk)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(isinstance(response.data, basestring))
        self.assertEqual(response.data, self.attachment.secure_url())
