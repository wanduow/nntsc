# This file is part of NNTSC
#
# Copyright (C) 2013 The University of Waikato, Hamilton, New Zealand
# Authors: Shane Alcock
#          Brendon Jones
#          Nathan Overall
#
# All rights reserved.
#
# This code has been developed by the WAND Network Research Group at the
# University of Waikato. For more information, please see
# http://www.wand.net.nz/
#
# This source code is proprietary to the University of Waikato and may not be
# redistributed, published or disclosed without prior permission from the
# University of Waikato and the WAND Network Research Group.
#
# Please report any bugs, questions or comments to contact@wand.net.nz
#
# $Id$


from sqlalchemy import create_engine, Table, Column, Integer, \
        String, MetaData, ForeignKey, UniqueConstraint, event, DDL, Index
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.sql import and_, or_, not_, text
from sqlalchemy.sql.expression import select, outerjoin, func, label
from sqlalchemy.engine.url import URL
from sqlalchemy.engine import reflection

import time, sys

from sqlalchemy.schema import DDLElement, DropTable, ForeignKeyConstraint, \
        DropConstraint, Table
from sqlalchemy.sql import table
from sqlalchemy.ext import compiler

from libnntscclient.logger import *

class CreateView(DDLElement):
    def __init__(self, name, selectable):
        self.name = name
        self.selectable = selectable

class DropView(DDLElement):
    def __init__(self, name):
        self.name = name

@compiler.compiles(CreateView)
def compile(element, compiler, **kw):
    return "CREATE VIEW %s AS %s" % (element.name, compiler.sql_compiler.process(element.selectable))

@compiler.compiles(DropView)
def compile(element, compiler, **kw):
    return "DROP VIEW %s" % (element.name)

class Database:
    def __init__(self, dbname, dbuser, dbpass=None, dbhost=None, \
            new=False, debug=False):

        #no host means use the unix socket
        if dbhost == "":
            dbhost = None

        if dbpass == "":
            dbpass = None

        connect_string = URL('postgresql', username=dbuser, password=dbpass, \
                host=dbhost, database=dbname)

        if debug:
            log('Connecting to db using "%s"' % connect_string)

        self.init_error = False
        self.dbname = dbname
        self.engine = create_engine(connect_string, echo=debug,
                implicit_returning = False)

        self.__reflect_db()

        self.conn = self.engine.connect()

        #self.stream_tables = {}
        #self.data_tables = {}

        #for name, tab in self.meta.tables.items():
        #    if name[0:5] == "data_":
        #        self.data_tables[name] = tab
        #    if name[0:8] == "streams_":
        #        self.stream_tables[name] = tab

        self.trans = self.conn.begin()
        self.pending = 0

    def __reflect_db(self):
        self.metadata = MetaData(self.engine)
        try:
            self.metadata.reflect(bind=self.engine)
        except OperationalError, e:
            log("Error binding to database %s" % (self.dbname))
            log("Are you sure you've specified the right database name?")
            self.init_error = True
            sys.exit(1)

        # reflect() is supposed to take a 'views' argument which will
        # force it to reflects views as well as tables, but our version of
        # sqlalchemy didn't like that. So fuck it, I'll just reflect the
        # views manually
        inspector = reflection.Inspector.from_engine(self.engine)
        views = inspector.get_view_names()
        for v in views:
            view_table = Table(v, self.metadata, autoload=True)


    def __del__(self):
        if not self.init_error:
            self.commit_transaction()
            self.conn.close()

    def create_view(self, name, query):

        t = table(name)

        for c in query.c:
            c._make_proxy(t)

        creator = DDL("CREATE VIEW %s AS %s" % (name, str(query.compile())))
        event.listen(self.metadata, 'after_create', creator)

        dropper = DDL("DROP VIEW %s" % (name))
        event.listen(self.metadata, 'before_drop', dropper)

        #CreateView(name, query).execute_at('after-create', self.metadata)
        #DropView(name).execute_at('before-drop', self.metadata)

        return t


    def build_databases(self, modules, new=False):
        if new:
            self.__delete_everything(self.engine)
            self.__reflect_db()

        if 'collections' not in self.metadata.tables:
            collections = Table('collections', self.metadata,
                Column('id', Integer, primary_key=True),
                Column('module', String, nullable=False),
                Column('modsubtype', String, nullable=True),
                Column('streamtable', String, nullable=False),
                Column('datatable', String, nullable=False),
                UniqueConstraint('module', 'modsubtype')
            )
            collections.create()

        if 'streams' not in self.metadata.tables:
            streams = Table('streams', self.metadata,
                Column('id', Integer, primary_key=True),
                Column('collection', Integer, ForeignKey('collections.id'),
                        nullable=False),
                Column('name', String, nullable=False),
                Column('lasttimestamp', Integer, nullable=False),
                Column('firsttimestamp', Integer, nullable=True),
            )

            streams.create()

            Index('index_streams_collection', streams.c.collection)

        # Create a useful function to select a mode from any data
        # http://scottrbailey.wordpress.com/2009/05/22/postgres-adding-custom-aggregates-most/
        mostfunc = text("""
            CREATE OR REPLACE FUNCTION _final_most(anyarray)
                RETURNS anyelement AS
            $BODY$
                SELECT a
                FROM unnest($1) a
                GROUP BY 1 ORDER BY count(1) DESC
                LIMIT 1;
            $BODY$
                LANGUAGE 'sql' IMMUTABLE;""")
        self.conn.execute(mostfunc)

        # we can't check IF EXISTS or use CREATE OR REPLACE, so just query it
        mostcount = self.conn.execute(
                """SELECT * from pg_proc WHERE proname='most';""")
        assert(mostcount.rowcount <= 1)

        # if it doesn't exist, create the aggregate function that applies
        # _final_most to multiple rows of data
        if mostcount.rowcount == 0:
            aggfunc = text("""
                CREATE AGGREGATE most(anyelement) (
                    SFUNC=array_append,
                    STYPE=anyarray,
                    FINALFUNC=_final_most,
                    INITCOND='{}'
                );""")
            self.conn.execute(aggfunc)

        for base, mod in modules.items():
            mod.tables(self)

        self.metadata.create_all()
        self.commit_transaction()

    def register_collection(self, mod, subtype, stable, dtable):
        table = self.metadata.tables['collections']

        try:
            self.conn.execute(table.insert(), module=mod, modsubtype=subtype,
                    streamtable=stable, datatable=dtable)
        except IntegrityError, e:
            self.rollback_transaction()
            log("Failed to register collection for %s:%s, probably already exists" % (mod, subtype))
            #print >> sys.stderr, e
            return -1

        self.commit_transaction()

    def register_new_stream(self, mod, subtype, name, ts):

        # Find the appropriate collection id
        coltable = self.metadata.tables['collections']

        sql = coltable.select().where(and_(coltable.c.module==mod,
                coltable.c.modsubtype==subtype))
        result = sql.execute()

        if result.rowcount == 0:
            log("Database Error: no collection for %s:%s" % (mod, subtype))
            return -1, -1

        if result.rowcount > 1:
            log("Database Error: duplicate collections for %s:%s" % (mod, subtype))
            return -1, -1

        col = result.fetchone()
        col_id = col['id']
        result.close()

        # Insert entry into the stream table
        sttable = self.metadata.tables['streams']

        try:
            result = self.conn.execute(sttable.insert(), collection=col_id,
                    name=name, lasttimestamp=0, firsttimestamp=ts)
        except IntegrityError, e:
            log("Failed to register stream %s for %s:%s, probably already exists" % (name, mod, subtype))
            #print >> sys.stderr, e
            return -1, -1

        # Return the new stream id
        newid = result.inserted_primary_key
        result.close()

        return col_id, newid[0]

    def __delete_everything(self, engine):
        #self.meta.drop_all(bind=engine)

        newmeta = MetaData()

        tbs = []
        all_fks = []
        views = []
        partitions = []

        inspector = reflection.Inspector.from_engine(self.engine)
        for table_name in inspector.get_table_names():
            fks = []
            for fk in inspector.get_foreign_keys(table_name):
                if not fk['name']:
                    continue
                fks.append(
                    ForeignKeyConstraint((), (), name=fk['name'])
                    )
            t = Table(table_name, newmeta, *fks)
            if table_name[0:5] == "part_":
                partitions.append(t)
            else:
                tbs.append(t)
            all_fks.extend(fks)

        for v in inspector.get_view_names():
            self.conn.execute(DropView(v))

        for fkc in all_fks:
            self.conn.execute(DropConstraint(fkc))

        for table in partitions:
            self.conn.execute(DropTable(table))

        for table in tbs:
            self.conn.execute(DropTable(table))

        self.commit_transaction()

    def list_collections(self):
        collections = []

        table = self.metadata.tables['collections']

        result = table.select().execute()
        for row in result:

            col = {}
            for k, v in row.items():
                col[k] = v
            collections.append(col)

        return collections

    def get_collection_schema(self, col_id):

        table = self.metadata.tables['collections']

        result = select([table.c.streamtable, table.c.datatable]).where(table.c.id ==col_id).execute()
        for row in result:
            stream_table = self.metadata.tables[row[0]]
            data_table = self.metadata.tables[row[1]]
            return stream_table.columns, data_table.columns

    def select_streams_by_module(self, mod):

        # Find all streams matching a given module type

        # For each stream:
        #   Form a dictionary containing all the relevant information about
        #   that stream (this will require info from both the combined streams
        #   table and the module/subtype specific table

        # Put all the dictionaries into a list

        col_t = self.metadata.tables['collections']
        streams_t = self.metadata.tables['streams']

        # Find the collection matching the given module
        sql = col_t.select().where(col_t.c.module == mod)
        result = sql.execute()

        stream_tables = {}

        for row in result:
            stream_tables[row['id']] = (row['streamtable'], row['modsubtype'])
        result.close()

        streams = []
        for cid, (tname, sub) in stream_tables.items():
            t = self.metadata.tables[tname]
            sql = t.join(streams_t, streams_t.c.id == t.c.stream_id).select().where(streams_t.c.collection==cid)
            result = sql.execute()

            for row in result:
                row_dict = {"modsubtype":sub}
                for k, v in row.items():
                    if k == 'id':
                        continue
                    row_dict[k] = v
                streams.append(row_dict)
            result.close()
        return streams

    def select_streams_by_collection(self, coll, minid):

        coll_t = self.metadata.tables['collections']
        streams_t = self.metadata.tables['streams']

        selected = []

        sql = coll_t.select().where(coll_t.c.id == coll)
        result = sql.execute()

        assert(result.rowcount == 1)
        coldata = result.fetchone()

        colstrtable = self.metadata.tables[coldata['streamtable']]

        sql = select([colstrtable, streams_t]).select_from(colstrtable.join(streams_t, streams_t.c.id == colstrtable.c.stream_id)).where(colstrtable.c.stream_id > minid)
        result = sql.execute()

        for row in result:
            stream_dict = {}
            for k, v in row.items():
                if k == "id":
                    continue
                stream_dict[k] = v
            selected.append(stream_dict)
        result.close()
        return selected

    def commit_transaction(self):
        # TODO: Better error handling!

        #print "Committing %d statements (%s)" % (self.pending, \
        #        time.strftime("%d %b %Y %H:%M:%S", time.localtime()))
        try:
            self.trans.commit()
        except:
            self.trans.rollback()
            raise
        self.trans = self.conn.begin()

    def rollback_transaction(self):
        #if self.pending == 0:
        #    return
        self.trans.rollback()
        self.trans = self.conn.begin()

    def update_timestamp(self, stream_id, lasttimestamp):
        table = self.metadata.tables['streams']
        result = self.conn.execute(table.update().where( \
                table.c.id==stream_id).values( \
                lasttimestamp=lasttimestamp))
        result.close()
        self.pending += 1

    def set_firsttimestamp(self, stream_id, ts):
        table = self.metadata.tables['streams']
        result = self.conn.execute(table.update().where( \
                table.c.id==stream_id).values( \
                firsttimestamp=ts))
        result.close()
        self.pending += 1


# vim: set sw=4 tabstop=4 softtabstop=4 expandtab :
