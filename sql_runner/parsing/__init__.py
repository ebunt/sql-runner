import sqlparse
import re
from functools import lru_cache
from typing import List, Iterator, Union, Callable

class IncompatibleSQLError(Exception):
    pass

class Query():
    """ Handles a full tokenized query, with specifications about identifier quoting
    """

    """ Patterns for matching the list of source relations in a DML query
    """
    source_patterns = (
        # Pattern for finding "FROM/JOIN x.y.z, a.b, c" or similar
        re.compile(r'(f[\s-]*)((?:(?:n\.){,2}n)(?:[\s-]*,[\s-]*(?:n\.){,2}n)*)'),
        # Pattern for finding individual items from comma-separated entities from the string matched by the
        # previous pattern
        re.compile(r'(?<![\.n])(?:n\.){,2}n(?![\.n])')
    )

    """ Pattern for matching the entire DDL part of a CREATE SOMETHING <name> AS ...
    """
    ddl_pattern = re.compile(r'^([\s-]*d.+?a[\s-]*)(?=[cs])')

    """ Pattern for recognizing whether this query contains any data manipulation language (WITH... SELECT...)
    """
    dml_pattern = re.compile(r'[cs]')

    def __init__(self, tokens: List[sqlparse.sql.Token],
                 start_quotes: str='"', end_quotes: str='"'):
        self.tokens: List[sqlparse.sql.Token] = tokens
        self.start_quotes: str = start_quotes
        self.end_quotes: str = end_quotes

    @staticmethod
    def get_queries(statement: str, start_quotes: str='"', end_quotes: str='"') -> Iterator["Query"]:
        """ Gets the Query objects from a string SQL statement
        """
        stmts = sqlparse.parse(statement)
        for stmt in stmts:
            yield Query(list(stmt.flatten()), start_quotes, end_quotes)

    @property
    @lru_cache(maxsize=1)
    def __tokens_as_str(self) -> str:
        """ Converts the token list into a simplified string where each character is a token.
        This can then be parsed more reliably by regexp, and character position matches token position in the list.
        """
        chrtokens = []
        for token in self.tokens:
            ttype = token.ttype
            token_value = token.value.upper()
            if token.is_whitespace:
                chrtokens.append(' ')
            elif ttype in sqlparse.tokens.Punctuation:
                chrtokens.append(token.value)
            elif ttype in sqlparse.tokens.Keyword:
                if 'JOIN' in token_value or 'FROM' in token_value:
                    chrtokens.append('f')
                elif token_value == 'SELECT':
                    chrtokens.append('s')
                elif ttype in sqlparse.tokens.Keyword.CTE:
                    chrtokens.append('c')
                elif token_value == 'AS':
                    chrtokens.append('a')
                elif ttype in sqlparse.tokens.Keyword.DML:
                    chrtokens.append('m')
                elif ttype in sqlparse.tokens.Keyword.DDL:
                    chrtokens.append('d')
                else:
                    chrtokens.append('k')
            elif ttype in sqlparse.tokens.Name or ttype in sqlparse.tokens.Literal.String.Symbol:
                # This is a placeholder for a "name", a source
                # TODO: if not claling the flatten() method on the statement, we don't have to deal with individual
                # cases like Identifier, Name, whether it's all quoted or quoted individual pieces. All of it becomes
                # known as Identifier. But without Flatten, the parsing would have to be a bit more complex,
                # with recursion, references to pieces of the query, but it should work in the next iteration just fine
                chrtokens.append('n')
            elif ttype in sqlparse.tokens.Comment:
                chrtokens.append('-')
            elif ttype in sqlparse.tokens.Operator:
                chrtokens.append('o')
            else:
                chrtokens.append('?')
        return ''.join(chrtokens)

    @property
    @lru_cache(maxsize=1)
    def has_dml(self) -> bool:
        """ Does this query contain any DML?
        """
        return Query.dml_pattern.search(self.__tokens_as_str) is not None

    @property
    @lru_cache(maxsize=1)
    def sources(self) -> Iterator["Source"]:
        """ Returns all the sources for a DML query
        """
        pattern, local_pattern = tuple(Query.source_patterns)
        for m in pattern.finditer(self.__tokens_as_str):
            span = m.span()
            offset = len(m.group(1))
            for ml in local_pattern.finditer(m.group(2)):
                local_span = ml.span()
                yield Source(self, span[0] + local_span[0] + offset, span[0] + local_span[1] + offset)

    @property
    @lru_cache(maxsize=1)
    def without_ddl(self) -> "Query":
        """ Strips the DDL header of the query, for queries like CREATE TABLE ... AS SELECT
        """
        pattern = Query.ddl_pattern
        matches = []
        for m in pattern.finditer(self.__tokens_as_str):
            span = m.span()
            offset = len(m.group(1))
            matches.append((span[0], span[0] + offset))
        matches.sort(key=lambda m: m[0])
        tokens = []
        last_end = 0
        for start, end in matches:
            tokens += self.tokens[last_end:start]
            last_end = end
        tokens += self.tokens[last_end:]
        return Query(tokens)
    
    def __str__(self) -> str:
        return str(sqlparse.sql.TokenList(self.tokens))


class QueryPart:
    def __init__(self, query: Query, start: int, end: int):
        self._start = start
        self._end = end
        self.query = query

    def __str__(self):
        return str(sqlparse.sql.TokenList(self.query.tokens[self._start:self._end]))


class NameTokenWrapper(QueryPart):
    """ Wraps a single Name token, for easy and centralized value manipulation and quoting automation
    """
    def __init__(self, query: Query, index: int):
        super().__init__(query, index, index + 1)
        self.token = query.tokens[index]
        self.index = index
        _, self.quote_index = self.clean_name(self.token.value)

    @property
    def value(self):
        return self.clean_name(self.token.value)[0]

    @value.setter
    def value(self, val):
        idx = self.quote_index
        if idx:
            self.token.value = self.query.start_quotes[idx] + val + self.query.end_quotes[idx]
        else:
            self.token.value = val

    def clean_name(self, name: str) -> str:
        """ Strips any kind of SQL quotes from around identifiers
        """
        start_quotes = self.query.start_quotes
        end_quotes = self.query.end_quotes
        if name[0] in start_quotes:
            which_quote = start_quotes.find(name[0])
            if name[-1] == end_quotes[which_quote]:
                return name[1:-1], which_quote
        return name, None


class PartialNameTokenWrapper():
    """ Used to represent a single identifier from a compound Name token, like which Google BigQuery uses.
    """
    def __init__(self, name_token_wrapper: NameTokenWrapper, start: int, end: int,
                 right_neighbors: List["PartialNameTokenWrapper"]):
        self.__name_token_wrapper = name_token_wrapper
        # Notify right neighbors of a length change, so they re-compute the start and end accordingly
        self.__right_neighbors = right_neighbors
        self.__start = start
        self.__end = end
        self.__last_known_full_length = len(self.name_token_wrapper.value)
    
    @property
    def name_token_wrapper(self):
        """ The full token for the full name
        """
        # Make it read-only
        return self.__name_token_wrapper

    @property
    def value(self) -> str:
        return self.name_token_wrapper.value[self.__start:self.__end]

    @value.setter
    def value(self, val: str):
        old_wrapper_value = self.name_token_wrapper.value
        new_end = self.__start + len(val)
        self.name_token_wrapper.value = old_wrapper_value[:self.__start] + val + old_wrapper_value[self.__end:]
        self.__end = self.__start + len(val)
        for neighbor in self.__right_neighbors:
            neighbor.update_position()

    def __len__(self) -> int:
        return self.__end - self.__start

    def update_position(self):
        """ Update known positions of part, when a left neighbor has changed the length of the full token
        """
        current_full_length = len(self.name_token_wrapper.value)
        if current_full_length != self.__last_known_full_length:
            diff = current_full_length - self.__last_known_full_length
            # positive means that all items switched to the right
            self.__start += diff
            self.__end += diff

    @staticmethod
    def get_from_token_wrapper(token_wrapper: NameTokenWrapper) -> Iterator["PartialNameTokenWrapper"]:
        val = token_wrapper.value
        end = len(val)
        partial_values = reversed(val.split('.'))
        partial_name_token_wrappers = []
        for partial_value in partial_values:
            # TODO: This is buggy. Won't see proper tokens
            partial_name_token_wrapper = PartialNameTokenWrapper(
                token_wrapper,
                # TODO: This is wrong
                end - len(partial_value),
                end,
                tuple(partial_name_token_wrappers)
            )
            # the partial name
            end -= len(partial_value)
            # the dot
            end -= 1
            partial_name_token_wrappers.append(partial_name_token_wrapper)
        return reversed(partial_name_token_wrappers)

class Source(QueryPart):
    """ A compound identifier that represents a data source. Usually as a token list of <schema_name><dot><table_name>
    """
    def __init__(self, query: Query, start: int, end: int):
        super().__init__(query, start, end)
        self.tokens = query.tokens[start:end]
        self.__individual_names = False

        # Determine what type of tokens we have, and wrap them in handler classes
        names = []
        index = 0
        for token in self.tokens:
            if token.ttype in sqlparse.tokens.Name or token.ttype in sqlparse.tokens.Literal.String.Symbol:
                name_part = NameTokenWrapper(query, start + index)
                names.append(name_part)
            elif token.ttype in sqlparse.tokens.Punctuation and token.value == '.':
                self.__individual_names = True
            index += 1
        if not self.__individual_names:
            names = list(PartialNameTokenWrapper.get_from_token_wrapper(names[0]))

        self.__relation: PartialNameTokenWrapper = names.pop()
        self.__schema: PartialNameTokenWrapper = names.pop() if names else None
        self.__database: PartialNameTokenWrapper = names.pop() if names else None

    @property
    def relation(self) -> str:
        return self.__relation.value

    @relation.setter
    def relation(self, value: str):
        self.__relation.value = value

    @property
    def schema(self) -> Union[str, None]:
        if self.__schema:
            return self.__schema.value

    @schema.setter
    def schema(self, value: str):
        if self.__schema:
            self.__schema.value = value
        else:
            raise IncompatibleSQLError("Can't edit the schema where none is present in original query. "
                                       "Not yet supported.")

    @property
    def database(self) -> Union[str, None]:
        if self.__database:
            return self.__database.value

    @database.setter
    def database(self, value: str):
        if self.__database:
            self.__database.value = value
        raise IncompatibleSQLError("Can't edit the database where none is present in original query. "
                                   "Not yet supported.")
