from fastapi import APIRouter, Path, Query, Response
from core import run
from app.scenarios.notes import CreateNoteDTO, NoteIdDTO, ListNotesDTO, NoteResponse, NoteListResponse

router = APIRouter(tags=['notes'])


@router.post('/notes', status_code=201, response_model=NoteResponse)
async def create_note(dto: CreateNoteDTO):
    return await run('notes.create', dto)


@router.get('/notes', response_model=NoteListResponse)
async def list_notes(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await run('notes.list', ListNotesDTO(limit=limit, offset=offset))


@router.get('/notes/{note_id}', response_model=NoteResponse)
async def get_note(note_id: int = Path(gt=0)):
    return await run('notes.get', NoteIdDTO(note_id=note_id))


@router.delete('/notes/{note_id}', status_code=204)
async def delete_note(note_id: int = Path(gt=0)):
    await run('notes.delete', NoteIdDTO(note_id=note_id))
    return Response(status_code=204)
