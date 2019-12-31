import larry.mturk
import larry.utils
import collections


class HIT(collections.UserDict):

    def __init__(self, data, mturk_client=None, production=None):
        self.__client = mturk_client
        collections.UserDict.__init__(self)
        if isinstance(data, str):
            hit, prod = larry.mturk._get_hit(data, mturk_client)
            self.update(hit)
            self['Production'] = prod
        else:
            self.update(data)
            self._parse_datetime_values()
            if production is not None:
                self['Production'] = production

    def _parse_datetime_values(self):
        for key in ['CreationTime', 'Expiration']:
            try:
                if key in self and isinstance(self[key], str):
                    self[key] = larry.utils.parse_date(self[key])
            except ValueError:
                pass

    def __repr__(self):
        return "{}('{}')".format(type(self).__name__, self.hit_id)

    def __str__(self):
        return "<{}: {}>".format(self.hit_id, self.status)

    def refresh(self):
        self.update(larry.mturk._get_hit(self.hit_id, self.__client))
        if 'Assignments' in self:
            self.retrieve_assignments()

    def retrieve_assignments(self):
        self['Assignments'] = list(larry.mturk.list_assignments_for_hit(self.hit_id))
        return self['Assignments']

    def retrieve_annotation(self, s3_resource=None):
        self['Annotation'] = larry.mturk.parse_requester_annotation(self.get('RequesterAnnotation'),
                                                                    s3_resource=s3_resource)

    def __missing__(self, key):
        if key == 'Assignments':
            self.retrieve_assignments()
            return self['Assignments']
        elif key == 'Annotation':
            self.retrieve_annotation()
            return self['Annotation']
        else:
            raise KeyError(key)

    @property
    def assignments(self):
        return self['Assignments']

    @property
    def hit_id(self):
        return self['HITId']

    @property
    def reward(self):
        return float(self['Reward'])

    @property
    def reward_cents(self):
        return float(self['Reward']) * 100

    @property
    def hit_type_id(self):
        return self['HITTypeId']

    @property
    def hit_group_id(self):
        return self['HITGroupId']

    @property
    def hit_layout_id(self):
        return self.get('HITLayoutId', None)

    @property
    def creation_time(self):
        return self['CreationTime']

    @property
    def title(self):
        return self['Title']

    @property
    def description(self):
        return self['Description']

    @property
    def question(self):
        return self.get('Question', None)

    @property
    def keywords(self):
        return self['Keywords']

    @property
    def status(self):
        return self['HITStatus'] #: 'Assignable' | 'Unassignable' | 'Reviewable' | 'Reviewing' | 'Disposed',

    @property
    def max_assignments(self):
        return self.get('MaxAssignments', None)

    @property
    def auto_approval_delay(self):
        return self.get('AutoApprovalDelayInSeconds', None)

    @property
    def expiration(self):
        return self.get('Expiration', None)

    @property
    def duration(self):
        return self.get('AssignmentDurationInSeconds', None)

    @property
    def annotation(self):
        return self.get('Annotation', None)

    @property
    def qualification_requirements(self):
        return self.get('QualificationRequirements', None)

    @property
    def review_status(self):
        return self.get('HITReviewStatus', None)

    @property
    def pending(self):
        return self.get('NumberOfAssignmentsPending', None)

    @property
    def available(self):
        return self.get('NumberOfAssignmentsAvailable', None)

    @property
    def completed(self):
        return self.get('NumberOfAssignmentsCompleted', None)

    @property
    def production(self):
        return self.get('Production', None)

    @property
    def preview(self):
        return larry.mturk.preview_url(self.hit_type_id, self.production)