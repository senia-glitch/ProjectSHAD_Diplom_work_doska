from sqlmodel import Field, SQLModel


class Note(SQLModel, table=True):
    __tablename__ = 'notes'
    id: int | None = Field(default=None, primary_key=True)
    title: str = Field(max_length=200)


metadata = SQLModel.metadata


def get_all_schemas():
    return {'notes': Note}
