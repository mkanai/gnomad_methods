import argparse
from gnomad_hail.resources import *
from gnomad_hail.utils.generic import flip_base 
import logging
from os.path import dirname, basename
import sys


logging.basicConfig(format="%(asctime)s (%(name)s %(lineno)s): %(message)s", datefmt='%m/%d/%Y %I:%M:%S %p')
logger = logging.getLogger("liftover")
logger.setLevel(logging.INFO)


def get_checkpoint_path(gnomad: bool, data_type: str, table_path: str) -> str:
    """
    Creates a checkpoint path for Table
    :param bool gnomad: Whether data is gnomAD data
    :param str data_type: Data type (exomes or genomes for gnomAD; not used otherwise)
    :param str table_path: Path to input Table (if data is not gnomAD data)
    :return: Output checkpoint path
    :rtype: str
    """
    
    if gnomad:
        return f'gs://gnomad/liftover/release/2.1.1/ht/{data_type}/gnomad.{data_type}.r2.1.1.liftover.ht'
        #return get_gnomad_liftover_data_path(data_type, version=CURRENT_RELEASE)
    else:
        out_name = basename(table_path).split('.')[0]
        return f'{dirname(table_path)}/{out_name}_lift.ht'


def lift_ht(ht: hl.Table, gnomad: bool, data_type: str, table_path: str,  rg: hl.genetics.ReferenceGenome) -> hl.Table:
    """
    Lifts input Table from one reference build to another
    :param Table ht: Table
    :param bool gnomad: Whether data is gnomAD data
    :param str data_type: Data type (exomes or genomes for gnomAD; not used otherwise)
    :param str table_path: Path to input Table (if data is not gnomAD data)
    :param ReferenceGenome rg: Reference genome
    :return: Table with liftover annotations
    :rtype: Table
    """

    # annotate release file with liftover coordinates
    ht = ht.annotate(new_locus=hl.liftover(ht.locus, rg, include_strand=True),
                     old_locus=ht.locus
                    )
    no_target = ht.aggregate(hl.agg.count_where(hl.is_missing(ht.new_locus)))
    logger.info('Filtering out {} sites that failed to liftover'.format(no_target))
    ht = ht.filter(hl.is_defined(ht.new_locus)) 
    ht = ht.key_by(locus=ht.new_locus.result, alleles=ht.alleles)
    ht = ht.checkpoint(get_checkpoint_path(gnomad, data_type, table_path), overwrite=True)
    return ht


def annotate_snp_mismatch(ht: hl.Table, data_type: str, rg: hl.genetics.ReferenceGenome) -> hl.Table:
    """
    Annotates mismatches between reference allele and allele in reference fasta
    Assumes input Table has ht.new_locus annotation
    :param Table ht: Table of SNPs to be annotated
    :param str data_type: Data type (exomes or genomes for gnomAD; not used otherwise)
    :param ReferenceGenome rg: GRCh38 reference genome with fasta sequence loaded
    :return: Table annotated with mismatches between reference allele and allele in fasta
    :rtype: Table
    """
 
    # filter to snps
    ht = ht.filter(hl.is_snp(ht.alleles[0], ht.alleles[1]))

    # check if reference allele matches what is in reference fasta
    # for snps on negative strand, make sure reverse complement of ref allele matches what is in reference fasta
    ht = ht.annotate(
        reference_mismatch=hl.cond(
                                ht.new_locus.is_negative_strand,
                                (flip_base(ht.alleles[0]) != hl.get_sequence(ht.locus.contig, ht.locus.position, reference_genome=rg)),
                                (ht.alleles[0] != hl.get_sequence(ht.locus.contig, ht.locus.position, reference_genome=rg))
                                )
                )

    return ht

 
def check_mismatch(ht: hl.Table) -> hl.expr.expressions.StructExpression:
    """
    Checks for mismatches between reference allele and allele in reference fasta
    :param Table ht: Table to be checked
    :return: StructExpression containing counts for mismatches and count for all variants on negative strand
    :rtype: StructExpression
    """

    mismatch = ht.aggregate(hl.struct(total_variants=hl.agg.count(),
                                total_mismatch=hl.agg.count_where(ht.reference_mismatch),
                                negative_strand=hl.agg.count_where(ht.new_locus.is_negative_strand),
                                negative_strand_mismatch=hl.agg.count_where(ht.new_locus.is_negative_strand & ht.reference_mismatch)
                                )
                            )
    return mismatch


def main(args):

    hl.init()
    
    logger.info('Loading reference genomes, adding chain file, and loading fasta sequence for destination build')
    if args.build == 38:
        source = hl.get_reference('GRCh37')
        target = hl.get_reference('GRCh38')
        chain = 'gs://hail-common/references/grch37_to_grch38.over.chain.gz'
        target.add_sequence(
            'gs://hail-common/references/Homo_sapiens_assembly38.fasta.gz', 
            'gs://hail-common/references/Homo_sapiens_assembly38.fasta.fai'
            )
    else:
        source = hl.get_reference('GRCh38')
        target = hl.get_reference('GRCh37')
        chain = 'gs://hail-common/references/grch38_to_grch37.over.chain.gz'
        target.add_sequence(
            'gs://hail-common/references/human_g1k_v37.fasta.gz',
            'gs://hail-common/references/human_g1k_v37.fasta.fai'
            ) 
    source.add_liftover(chain, target)

    if args.gnomad:
        gnomad = True
        table_path = None
        if not args.exomes and not args.genomes:
            sys.exit('Error: Must specify --exomes or --genomes if lifting gnomAD ht')

        if args.exomes:
            data_type = 'exomes'
        if args.genomes:
            data_type = 'genomes'

        logger.info('Working on gnomAD {} release ht'.format(data_type))
        logger.info('Reading in release ht')
        ht = get_gnomad_public_data(data_type, split=True, version=CURRENT_RELEASE)
        logger.info('Variants in release ht: {}'.format(ht.count()))

    else:
        data_type = None
        gnomad = None
        table_path = args.table_path
        ht = hl.read_table(table_path)
   
    if args.test: 
        logger.info('Filtering to chr21 for testing')
        if args.build == 38:
            ht = ht.filter(ht.locus.contig == '21')
        else:
            ht = ht.filter(ht.locus.contig == 'chr21')

    logger.info('Lifting ht to b{}'.format(args.build))
    ht = lift_ht(ht, gnomad, data_type, table_path, target)
        
    logger.info('Checking SNPs for reference mismatches')
    ht = annotate_snp_mismatch(ht, data_type, target)

    mismatch = check_mismatch(ht)
    logger.info('{} total SNPs'.format(mismatch['total_variants']))
    logger.info('{} SNPs on minus strand'.format(mismatch['negative_strand']))
    logger.info('{} reference mismatches in SNPs'.format(mismatch['total_mismatch']))
    logger.info('{} mismatches on minus strand'.format(mismatch['negative_strand_mismatch']))


if __name__ == '__main__':

    # Create argument parser
    parser = argparse.ArgumentParser(description='This script lifts a ht from one build to another')
    parser.add_argument('-b', '--build', help='Destination build (37 or 38)', type=int, choices=[37, 38], default=38)
    parser.add_argument('-p', '--table_path', help='Full path to table for liftover')
    parser.add_argument('-g', '--gnomad', help='Liftover table is one of the gnomAD releases', action='store_true')
    parser.add_argument(
            '--exomes', 
            help='Data type is exomes. One of --exomes or --genomes is required if --gnomad is specified.', 
            action='store_true'
            )
    parser.add_argument(
            '--genomes', 
            help='Data type is genomes. One of --exomes or --genomes is required if --gnomad is specified.', 
            action='store_true'
            )
    parser.add_argument('-t', '--test', help='Filter to chr21 (for code testing purposes)', action='store_true')
    args = parser.parse_args()

    if args.gnomad and (int(args.exomes) + int(args.genomes) != 1):
        sys.exit('Error: One and only one of --exomes or --genomes must be specified')

    if not args.table_path and not args.gnomad:
        sys.exit('Error: One and only one of -p/--table_path or -g/--gnomad must be specified')

    main(args)