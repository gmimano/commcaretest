from couchdbkit.ext.django.schema import *
from django.core.urlresolvers import reverse
from corehq.util import bitly
from corehq.util.webutils import URL_BASE
from django.http import Http404
from copy import deepcopy
from corehq.apps.domain.models import Domain
from BeautifulSoup import BeautifulStoneSoup
from datetime import datetime

DETAIL_TYPES = ('case_short', 'case_long', 'ref_short', 'ref_long')

class XForm(Document):
    xmlns = StringProperty()
    submit_time = DateTimeProperty()
    domain = StringProperty()

class XFormGroup(Document):
    "Aggregate of all XForms with the same xmlns"
    display_name = StringProperty()
    xmlns = StringProperty()

class VersioningError(Exception):
    pass

class VersionedDoc(Document):
    domain = StringProperty()
    copy_of = StringProperty()
    version = IntegerProperty()
    short_url = StringProperty()

    @property
    def id(self):
        return self._id

    def save(self, **params):
        self.version = self.version + 1 if self.version else 1
        super(VersionedDoc, self).save()
        if not self.short_url:
            self.short_url = bitly.shorten(
                URL_BASE + reverse('corehq.apps.app_manager.views.download_jad', args=[self.domain, self._id])
            )
            super(VersionedDoc, self).save()
    def save_copy(self):
        copies = VersionedDoc.view('app_manager/applications', key=[self.domain, self._id, self.version]).all()
        if copies:
            copy = copies[0]
        else:
            copy = deepcopy(self.to_json())
            del copy['_id']
            del copy['_rev']
            del copy['short_url']
            cls = self.__class__
            copy = cls.wrap(copy)
            copy['copy_of'] = self._id
            copy.version -= 1
            copy.save()
        return copy
    def revert_to_copy(self, copy):
        """
        Replaces couch doc with a copy of the backup ("copy").
        Returns the another Application/RemoteApp referring to this
        updated couch doc. The returned doc should be used in place of
        the original doc, i.e. should be called as follows:
            app = revert_to_copy(app, copy)
        This is not ideal :(
        """
        if copy.copy_of != self._id:
            raise VersioningError("%s is not a copy of %s" % (copy, self))
        app = deepcopy(copy).to_json()
        app['_rev'] = self._rev
        app['_id'] = self._id
        app['version'] = self.version
        app['copy_of'] = None
        cls = self.__class__
        app = cls.wrap(app)
        app.save()
        return app

    def delete_copy(self, copy):
        if copy.copy_of != self._id:
            raise VersioningError("%s is not a copy of %s" % (copy, self))
        copy.delete()

class Application(VersionedDoc):
    modules = ListProperty()
    trans = DictProperty()
    name = DictProperty()
    langs = ListProperty()

    def get_modules(self):
        for i in range(len(self.modules)):
            yield Module(self, i)

    def get_module(self, i):
        return Module(self, i)

    @classmethod
    def new_app(cls, domain, name, lang="default"):
        app = cls(domain=domain, modules=[], name={lang: name}, langs=["default"])
        app.save()
        return app

    def new_module(self, name, lang="default"):
        self.modules.append({
            'name': {lang: name},
            'forms': [],
            'case_type': '',
            'case_name': {},
            'ref_name': {},
            'details': [{'type': detail_type, 'columns': []} for detail_type in DETAIL_TYPES],
        })
        self.save()
        return self.get_module(len(self.modules)-1)
    def delete_module(self, module_id):
        del self.modules[int(module_id)]
        self.save()

    def new_detail_header(self, module_id, detail_type):
        pass
    def update_detail_header(self, module_id, detail_type):
        pass

    def new_form(self, module_id, name, attachment, lang="default"):
        xform = _register_xform(self.domain, attachment=attachment)
        module = self.get_module(module_id)
        module['forms'].append({
            'name': {lang: name},
            'xform_id': xform._id,
            'xmlns': xform.xmlns
        })
        form = module.get_form(len(module['forms'])-1)
        self.save()
        return form
    def delete_form(self, module_id, form_id):
        module = self.get_module(module_id)
        del module['forms'][int(form_id)]
        self.save()

    def swap_langs(self, i, j):
        langs = self.langs
        langs.insert(i, langs.pop(j))
        self.langs = langs
        self.save()
    def swap_modules(self, i, j):
        modules = self.modules
        modules.insert(i, modules.pop(j))
        self.modules = modules
        self.save()
    def swap_details(self, module_id, detail_type, i, j):
        module = self.get_module(module_id)
        detail = module['details'][DETAIL_TYPES.index(detail_type)]
        columns = detail['columns']
        columns.insert(i, columns.pop(j))
        detail['columns'] = columns
        self.save()
    def swap_forms(self, module_id, i, j):
        forms = self.modules[module_id]['forms']
        forms.insert(i, forms.pop(j))
        self.modules[module_id]['forms'] = forms
        self.save()

class RemoteApp(VersionedDoc):
    profile_url = StringProperty()
    suite_url = StringProperty()
    name = DictProperty()

    @classmethod
    def new_app(cls, domain, name):
        app = cls(domain=domain, name={lang: name}, langs=["default"])
        app.save()
        return app



# The following classes are wrappers for the subparts of an application document
class DictWrapper(object):
    def __eq__(self, other):
        try:
            return (self.id == other.id) and (self.parent == other.parent)
        except:
            return False
def _call_dict(fn):
    def _fn(self, *args, **kwargs):
        return getattr(self._dict, fn)(*args, **kwargs)
    return _fn

for fn in ('__getitem__', '__setitem__', '__contains__', 'update', 'get'):
    setattr(DictWrapper, fn, _call_dict(fn))

class Module(DictWrapper):
    def __init__(self, app, id):
        self.app = app
        self.parent = self.app
        self.id = int(id)
        self._dict = app.modules[self.id]

    def get_forms(self):
        for i in range(len(self['forms'])):
            yield Form(self, i)
    def get_form(self, i):
        return Form(self, i)
    def get_detail(self, detail_type):
        for detail in self['details']:
            if detail['type'] == detail_type:
                break


class Form(DictWrapper):
    def __init__(self, module, id):
        self.module = module
        self.parent = self.module
        self.id = int(id)
        self._dict = module['forms'][self.id]

class DomainError(Exception):
    pass



def get_app(domain, app_id):
    app = VersionedDoc.get(app_id)

    try:    Domain.objects.get(name=domain)
    except: raise DomainError("domain %s does not exist" % domain)

    if app.domain != domain:
        raise DomainError("%s not in domain %s" % (app._id, domain))
    cls = {'Application': Application, "RemoteApp": RemoteApp}[app.doc_type]
    app = cls.wrap(app.to_json())
    return app

def _register_xform(domain, attachment):
    if not isinstance(attachment, basestring):
        attachment = attachment.read()
    xform = XForm()
    soup = BeautifulStoneSoup(attachment)
    xform.xmlns = soup.find('instance').findChild()['xmlns']

    xform.submit_time = datetime.utcnow()
    xform.domain = domain

    xform.save()
    xform.put_attachment(attachment, 'xform.xml', content_type='text/xml')
    return xform