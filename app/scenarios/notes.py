from pydantic import BaseModel, Field
from core import BaseScenario, NotFoundError, register_scenario


class CreateNoteDTO(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class NoteIdDTO(BaseModel):
    note_id: int = Field(gt=0)


class ListNotesDTO(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class NoteResponse(BaseModel):
    id: int
    title: str


class NoteListResponse(BaseModel):
    items: list[NoteResponse]
    total: int
    limit: int
    offset: int


@register_scenario('notes.create', dto=CreateNoteDTO, response=NoteResponse)
class CreateNote(BaseScenario):
    async def execute(self, dto):
        return NoteResponse(**await self._db.create('notes', dto.model_dump()))


@register_scenario('notes.get', dto=NoteIdDTO, response=NoteResponse)
class GetNote(BaseScenario):
    async def execute(self, dto):
        row = await self._db.read('notes', dto.note_id)
        if row is None:
            raise NotFoundError('Note not found')
        return NoteResponse(**row)


@register_scenario('notes.list', dto=ListNotesDTO, response=NoteListResponse)
class ListNotes(BaseScenario):
    async def execute(self, dto):
        rows = await self._db.query('SELECT id, title FROM notes ORDER BY id LIMIT :limit OFFSET :offset', dto.model_dump())
        total = await self._db.query('SELECT count(*) AS total FROM notes', {})
        return NoteListResponse(items=rows, total=total[0]['total'], **dto.model_dump())


@register_scenario('notes.delete', dto=NoteIdDTO)
class DeleteNote(BaseScenario):
    async def execute(self, dto):
        if not await self._db.delete('notes', dto.note_id):
            raise NotFoundError('Note not found')
